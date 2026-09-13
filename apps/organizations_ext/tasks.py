import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Greatest
from django.tasks import task
from django.utils import timezone

from apps.issue_events.maintenance import (
    delete_issues_in_batches,
    raw_delete_in_batches,
)
from apps.issue_events.models import Issue, IssueAggregate, IssueEvent, IssueTag
from apps.logs.models import LogEvent
from apps.projects.models import (
    IssueEventProjectHourlyStatistic,
    LogProjectHourlyStatistic,
    TransactionEventProjectHourlyStatistic,
)
from apps.stripe.exceptions import StripeError
from apps.stripe.overage import (
    METER_FORMULA,
    billable_units,
    cycle_quota,
    overage_eligible,
    units_for_budget,
)
from apps.stripe.utils import billing_window, has_metered_window
from apps.uptime.models import MonitorCheck, UptimeCheckHourlyStatistic

from .email import InvitationEmail, ThrottleNoticeEmail
from .models import (
    Organization,
    get_event_counts,
    get_free_tier_cycle,
)

logger = logging.getLogger(__name__)


@task
async def update_subscription_cycles():
    """Delegate to maintenance function — kept as task for backwards compatibility."""
    from apps.stripe.maintenance import update_subscription_cycles as _update

    await _update()


@task
async def check_organization_throttle(organization_id: int, bypass_cache: bool = False):
    if not bypass_cache and not await cache.aadd(
        f"org-throttle-{organization_id}", True
    ):
        return  # Recent check already performed

    org = await Organization.objects.select_related(
        "stripe_primary_subscription__price__product"
    ).aget(id=organization_id)
    await _check_and_update_throttle(org)


@task
async def check_all_organizations_throttle():
    BATCH_SIZE = 500
    base_qs = Organization.objects.select_related(
        "stripe_primary_subscription__price__product"
    ).order_by("id")
    last_id = 0
    while True:
        orgs = await sync_to_async(list)(base_qs.filter(id__gt=last_id)[:BATCH_SIZE])
        if not orgs:
            break
        for org in orgs:
            try:
                await _check_and_update_throttle(org)
            except Exception:
                # One organization's failure (a cache blip, a row deleted
                # mid-sweep) must not stop throttling for every org after it.
                logger.exception("Throttle check failed for org %s", org.id)
        last_id = orgs[-1].id


def _progressive_throttle(usage: int, start: int, width: int) -> int:
    """Ramp 0 → 10 → 50 → 100 as usage climbs past ``start`` over ``width``.

    With ``start = width = quota`` this is the base behavior (over quota → 10%,
    1.5× → 50%, 2× → 100%); the metered path anchors it past the paid budget.
    """
    if usage > start + width:
        return 100
    if usage > start + width // 2:
        return 50
    if usage > start:
        return 10
    return 0


METER_FORMULA_CACHE_TTL = 3600
METER_FORMULA_RETRY_TTL = 300
METER_FORMULA_UNKNOWN_TTL = 60
METERED_PERIOD_REFRESH_TTL = 600


async def _refresh_metered_period(sub, measured_at) -> None:
    """Re-read the subscription from Stripe once its metered item's period is
    up, or was never recorded.

    Stripe owns that period (see ``billing_window``) and normally announces
    the next one by webhook. Until it lands the local window is stale, and a
    stale window drops every report and freezes the count the throttle is set
    by -- for an organization under quota, no throttle at all. Rather than
    advance the period by local arithmetic, which is a guess at Stripe's
    answer, ask Stripe: at most once every ten minutes per subscription, so a
    Stripe outage costs one call per sweep rather than one per check. A failed
    refresh (or an unreachable cache) leaves the window as it was: stale or
    unknown, either of which reports nothing until the period is known.
    """
    if not sub.metered_item_id:
        return
    if sub.metered_period_end and measured_at < sub.metered_period_end:
        return
    try:
        if not await cache.aadd(
            f"sub-period-refresh-{sub.stripe_id}", "1", METERED_PERIOD_REFRESH_TTL
        ):
            return
        await sub.refresh_from_stripe()
    except Exception:
        logger.warning(
            "Could not refresh subscription %s from Stripe; its metered period "
            "stays stale",
            sub.stripe_id,
            exc_info=True,
        )


async def _meter_reports_last() -> bool:
    """Whether the overage meter aggregates by ``last``.

    Every report is a cycle-to-date total. Into a ``sum`` meter those would add
    the whole cycle again on each sweep -- the one failure here that overcharges
    without bound, and one that a provisioning slip or a rollback of this code
    alone could produce. So the formula is read from Stripe (cached) and the send
    path fails closed: unknown or wrong means no report, never a wrong one. A
    good answer is kept for an hour; a wrong one for five minutes so a fix to the
    meter takes effect promptly; a failed lookup for one minute so a Stripe or
    cache outage is probed once, not once per organization per sweep.
    """
    from apps.stripe.client import find_meter

    event_name = settings.GLITCHTIP_OVERAGE_METER_EVENT_NAME
    cache_key = f"overage-meter-formula:{event_name}"
    try:
        formula = await cache.aget(cache_key)
        if formula is None:
            try:
                meter = await find_meter(event_name)
            except Exception:
                logger.exception("Could not read the overage meter %r", event_name)
                formula, ttl = "unknown", METER_FORMULA_UNKNOWN_TTL
            else:
                formula = meter.default_aggregation.formula if meter else "missing"
                ttl = (
                    METER_FORMULA_CACHE_TTL
                    if formula == METER_FORMULA
                    else METER_FORMULA_RETRY_TTL
                )
                if formula != METER_FORMULA:
                    logger.error(
                        "Overage meter %r aggregates by %r, not %r; not reporting",
                        event_name,
                        formula,
                        METER_FORMULA,
                    )
            await cache.aset(cache_key, formula, ttl)
    except Exception:
        logger.exception("Overage meter check failed; not reporting")
        return False
    return formula == METER_FORMULA


def _in_window(measured_at, cycle_start, cycle_end) -> bool:
    """Whether Stripe would file ``measured_at`` under the local cycle.

    Stripe timestamps are whole seconds, so the test is on the timestamp that
    would actually be sent. False means either the period advanced under this
    check (measurement before the start) or the local period is stale after a
    Stripe renewal (measurement past the end): both describe a cycle other
    than the one Stripe would file the figure under.
    """
    seconds = int(measured_at.timestamp())
    return cycle_start.timestamp() <= seconds < cycle_end.timestamp()


async def _report_overage(
    org: Organization,
    cycle_start,
    cycle_end,
    measured_at,
    overage_units: int,
    cap_units: int,
) -> None:
    """Report the cycle-to-date billable overage to the Stripe meter.

    The meter aggregates by ``last``, so each report carries the whole figure
    and any re-send is harmless: a retry, a concurrent sweep, or a replacement
    subscription all restate the same number. The figure is recomputed from
    current data every time and nothing previously sent is an input to it, so
    a wrong figure -- whatever put it there -- is replaced by the next sweep
    restating the right one, downwards included. There is no counter to reset,
    roll back, or migrate, and every failure leaves Stripe holding at most
    what was measured.

    Stripe files the event under the billing period containing ``timestamp``
    and, within a period, bills the one with the latest timestamp regardless of
    arrival order (verified against Stripe test mode, not assumed). The
    measurement time is sent rather than the send time so a slow sweep can't
    overtake a fresher one. A measurement outside the local period is dropped:
    the local dates lag Stripe's by a webhook, so a check that runs just after
    a boundary can still be counting the old cycle while its timestamp would
    file the figure under the new one -- the whole prior cycle, billed again.

    ``overage_units`` is usage above the cycle's quota and ``cap_units`` what
    the spend cap affords. Within a cycle the cap is floored at what has
    already been billed: lowering it stops growth from that point but doesn't
    retract billed usage, or dropping the cap to a dollar on the last day would
    forgive the month. The floor is on the cap, not on the figure sent, so it
    can never pin a figure the true total no longer supports.
    """
    from apps.stripe.client import create_meter_event

    if not org.stripe_customer_id:
        return
    seconds = int(measured_at.timestamp())
    if not _in_window(measured_at, cycle_start, cycle_end):
        logger.warning(
            "Dropping overage report for org %s: measured %s outside cycle %s to %s",
            org.id,
            measured_at,
            cycle_start,
            cycle_end,
        )
        return

    def in_cycle(at):
        return at is not None and cycle_start <= at < cycle_end

    def figure(last_units: int, last_at) -> int:
        return billable_units(
            overage_units, cap_units, last_units, last_at, cycle_start, cycle_end
        )

    # The caller's copy of the record is enough to know nothing new is due,
    # which is the common case; a stale copy only errs toward the locked re-read.
    if in_cycle(org.overage_last_reported_at) and (
        figure(org.overage_last_reported_units, org.overage_last_reported_at)
        == org.overage_last_reported_units
    ):
        return

    lock_key = f"overage-report-{org.stripe_customer_id}"
    if not await cache.aadd(lock_key, "1", 60):
        return
    try:
        last_units, last_at = await Organization.objects.values_list(
            "overage_last_reported_units", "overage_last_reported_at"
        ).aget(id=org.id)
        units = figure(last_units, last_at)
        if in_cycle(last_at):
            # Two measurements in the same second have no defined order at
            # Stripe, so only a strictly later one goes; and an unchanged figure
            # needs no restating. A lower one does: it is the correction.
            if int(last_at.timestamp()) >= seconds or units == last_units:
                return
        elif units <= 0:
            # A new cycle with nothing billable yet; Stripe bills it at zero.
            return
        if not await _meter_reports_last():
            return

        try:
            await create_meter_event(
                settings.GLITCHTIP_OVERAGE_METER_EVENT_NAME,
                org.stripe_customer_id,
                units,
                f"{org.stripe_customer_id}:{seconds}",
                timestamp=seconds,
            )
        except StripeError as e:
            # 429 is Stripe's one-call-per-customer limit tripping on a sweep
            # the lock didn't serialize; 400 is a same-second duplicate. Both
            # are restated by the next sweep, so neither is an error to page on.
            log = logger.warning if e.status in (400, 429) else logger.exception
            log(
                "Overage meter event failed for org %s (status=%s code=%s)",
                org.id,
                e.status,
                e.code,
            )
            return
        except Exception:
            logger.exception("Failed to report overage meter event for org %s", org.id)
            return
        # Conditional so a sweep that lost the lock race and measured earlier
        # can't overwrite a newer record with an older figure.
        updated = await Organization.objects.filter(
            Q(id=org.id)
            & (
                Q(overage_last_reported_at__isnull=True)
                | Q(overage_last_reported_at__lt=measured_at)
            )
        ).aupdate(
            overage_last_reported_units=units,
            overage_last_reported_at=measured_at,
        )
        if updated:
            org.overage_last_reported_units = units
            org.overage_last_reported_at = measured_at
    finally:
        await cache.adelete(lock_key)


async def _cycle_quota(org: Organization, cycle_start, plan_events: int) -> int:
    """Hold and return the quota this cycle's overage is measured against.

    See :func:`apps.stripe.overage.cycle_quota` for what the mark is for. Kept
    for every subscribed organization, not only while overage is on: the
    downgrade it protects against can precede the opt-in, and a mark that
    started at enablement would bill a whole cycle of within-quota usage the
    moment overage was switched on.

    Written only when the caller's copy is behind: once per cycle for most
    organizations, plus on plan changes. The write is one conditional
    statement, so two checks racing (the sweep's batch copy can be minutes old
    when an ingest re-check has already stamped the new cycle) can't lower the
    mark. Any other anchor is replaced: the caller has already established
    that ``cycle_start`` is the cycle its measurement falls in, so a record on
    a different cycle is either the previous one or a primary subscription
    that changed under the organization. An upgrade raises the mark, so
    overage accrued under the smaller plan is forgiven for the cycle -- the
    customer-favouring direction -- and the meter takes the lower restatement.
    """
    quota = cycle_quota(org, cycle_start, plan_events)
    if org.overage_cycle_start == cycle_start and quota == org.overage_cycle_quota:
        return quota
    quota_field = models.PositiveBigIntegerField()
    await Organization.objects.filter(id=org.id).aupdate(
        overage_cycle_quota=Case(
            When(
                overage_cycle_start=cycle_start,
                then=Greatest(
                    F("overage_cycle_quota"),
                    Value(plan_events, output_field=quota_field),
                ),
            ),
            default=Value(plan_events, output_field=quota_field),
        ),
        overage_cycle_start=cycle_start,
    )
    (
        org.overage_cycle_start,
        org.overage_cycle_quota,
    ) = await Organization.objects.values_list(
        "overage_cycle_start", "overage_cycle_quota"
    ).aget(id=org.id)
    return cycle_quota(org, cycle_start, plan_events)


async def _check_and_update_throttle(org: Organization):
    if not settings.BILLING_ENABLED:
        return

    total_event_count = 0

    if org.stripe_primary_subscription:
        sub = org.stripe_primary_subscription
        price = sub.price
        if price.no_throttle:
            org_throttle = 0
        else:
            # A hosted plan always carries a quota; NULL means the base price
            # points at a quota-less product, which is only reachable if a
            # subscription somehow has nothing but a metered item. Fall back to
            # the free tier instead of throttling the org to a standstill.
            plan_events = price.product.events or settings.GLITCHTIP_FREE_TIER_EVENTS

            measured_at = timezone.now()
            # One subscription row answers both "is overage eligible" and
            # "which window". Re-querying the primary pointer for the dates
            # (as the shared period helper does) could see it cleared between
            # the two reads and hand back the signup-anchored free-tier window
            # for a subscription this check still treats as billing.
            await _refresh_metered_period(sub, measured_at)
            start, end = billing_window(sub)
            counts = await get_event_counts(org.id, start, end)
            total_event_count = counts.total_event_count

            # The cycle's quota high-water mark is held for every subscribed
            # organization (see _cycle_quota), but only by a check whose
            # measurement falls in the cycle it read; a stale one must not
            # re-stamp a cycle the record has already left.
            quota = plan_events
            if _in_window(measured_at, start, end):
                quota = await _cycle_quota(org, start, plan_events)

            # Both flags are sticky DB state that outlives the plan that
            # justified them: a downgrade to a free plan keeps the subscription
            # active, and the primary-subscription pointer is only cleared
            # asynchronously. Without the eligibility test here the API refuses
            # to *enable* overage on such an org while this path keeps invoicing
            # it. `select_related` on both callers keeps this DB-free.
            # Reporting also waits for the item's own period to be known
            # (has_metered_window): a total counted over the plan's window
            # would be filed under the item's, and where the two differ that
            # bills the overlap twice. Until the refresh above succeeds the
            # organization is throttled at quota plus cap but nothing is sent.
            if (
                org.metered_billing_enabled
                and has_metered_window(sub)
                and overage_eligible(sub)
            ):
                # Paid overage: charge for usage above the cycle's quota up to
                # the spend cap, then resume the progressive ramp (no further
                # charges).
                cap_units = units_for_budget(org.overage_spend_cap_cents)
                await _report_overage(
                    org,
                    start,
                    end,
                    measured_at,
                    max(0, total_event_count - quota),
                    cap_units,
                )
                org_throttle = _progressive_throttle(
                    total_event_count, quota + cap_units, quota
                )
            else:
                org_throttle = _progressive_throttle(
                    total_event_count, plan_events, plan_events
                )
    else:
        # Free Tier - use anchored cycle dates
        plan_events = settings.GLITCHTIP_FREE_TIER_EVENTS

        start, end = get_free_tier_cycle(org.created)
        counts = await get_event_counts(org.id, start, end)
        total_event_count = counts.total_event_count

        org_throttle = _progressive_throttle(
            total_event_count, plan_events, plan_events
        )

    if org.event_throttle_rate != org_throttle:
        old_throttle = org.event_throttle_rate
        org.event_throttle_rate = org_throttle
        await org.asave(update_fields=["event_throttle_rate"])
        if org_throttle > old_throttle:
            await send_throttle_email.aenqueue(org.id, total_event_count)


@task
async def send_throttle_email(organization_id: int, total_event_count: int = 0):
    await sync_to_async(
        ThrottleNoticeEmail(
            pk=organization_id, total_event_count=total_event_count
        ).send_email
    )()


@task
async def send_email_invite(org_user_id: int, token: str):
    await sync_to_async(InvitationEmail(pk=org_user_id, token=token).send_email)()


@task
async def delete_organization(organization_id: int):
    """Delete cold storage files for an org, then hard-delete from DB.

    Batch-deletes rows from partitioned tables first to avoid exhausting
    the PostgreSQL shared lock table (each partition + index = one lock).
    """
    org = await Organization.objects.aget(id=organization_id)

    # Cancel billing in Stripe before destroying the org. A subscription left
    # active there keeps charging the customer for an org that no longer exists.
    # Done first so a Stripe outage retries the whole task before any data is
    # irreversibly deleted.
    if settings.BILLING_ENABLED:
        from apps.stripe.models import StripeSubscription

        await StripeSubscription.cancel_for_organization(org)

    # Delete cold storage files before removing DB rows
    await sync_to_async(_delete_org_cold_storage)(org.id)

    # Batch-delete from partitioned tables to keep lock counts low.
    # Django's cascade collector would lock every partition + index at once.
    for qs in [
        IssueEvent.objects.filter(organization_id=org.id),
        LogEvent.objects.filter(organization_id=org.id),
        MonitorCheck.objects.filter(organization_id=org.id),
    ]:
        await raw_delete_in_batches(qs)

    # Partitioned tables with composite PKs (no id field) — delete directly.
    # Filtered by organization_id so Postgres prunes to the org's hash bucket.
    # IssueAggregate/IssueTag have DB-level CASCADE from Issue, so must be
    # deleted before Issues to avoid lock escalation across their partitions.
    for model in [
        IssueAggregate,
        IssueTag,
        IssueEventProjectHourlyStatistic,
        TransactionEventProjectHourlyStatistic,
        LogProjectHourlyStatistic,
        UptimeCheckHourlyStatistic,
    ]:
        await sync_to_async(model.objects.filter(organization_id=org.id)._raw_delete)(
            "default"
        )

    # Issues have non-partitioned FK dependents (IssueHash, Comment, etc.)
    # that need explicit cleanup — use the issue-aware batch helper.
    await delete_issues_in_batches(Issue.objects.filter(project__organization=org))

    # Remaining relations are non-partitioned — safe for Django cascade.
    await sync_to_async(org.force_delete)()
    logger.info("Organization %s (id=%s) fully deleted", org.slug, org.id)


def _delete_org_cold_storage(org_id: int):
    from glitchtip.cold_storage import (
        delete_org_cold_storage,
        is_duckdb_available,
    )

    if not is_duckdb_available():
        return

    storage_prefixes = [
        "logs_logevent",
        "issue_events_issueevent",
        "performance_spans",
        "performance_spans_rollup",
    ]
    for storage_prefix in storage_prefixes:
        delete_org_cold_storage(org_id, storage_prefix)
