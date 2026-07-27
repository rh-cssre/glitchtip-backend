import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.tasks import task

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
from apps.uptime.models import MonitorCheck, UptimeCheckHourlyStatistic

from .email import InvitationEmail, ThrottleNoticeEmail
from .models import (
    Organization,
    get_current_period_dates,
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
            await _check_and_update_throttle(org)
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


async def _report_overage(
    org: Organization, sub, cycle_start, billable_units: int
) -> None:
    """Report cumulative billable overage to Stripe as an additive delta.

    Meter events sum per cycle, so we only send the increase since the last
    report; the counter resets on cycle rollover. A per-subscription cache lock
    serializes concurrent throttle checks so they can't report overlapping
    deltas and over-charge.

    Ordering makes every failure window err toward undercharging: the counter
    is advanced *before* the send (so the DB always bounds what Stripe may have
    recorded — a crash can strand an unbilled delta, never re-bill one) and
    rolled back only on a definite send failure. The meter-event identifier is
    keyed on the base counter the delta was computed from, so a re-send racing
    a stale writer carries the same identifier and Stripe's uniqueness window
    absorbs it.
    """
    from apps.stripe.client import create_meter_event
    from apps.stripe.models import StripeSubscription

    if not org.stripe_customer_id:
        return

    lock_key = f"overage-report-{sub.stripe_id}"
    if not await cache.aadd(lock_key, "1", 60):
        return
    try:
        reported, period_start = await StripeSubscription.objects.values_list(
            "overage_units_reported", "overage_period_start"
        ).aget(stripe_id=sub.stripe_id)
        if period_start != cycle_start:
            reported = 0  # new cycle: counter resets

        delta = billable_units - reported
        if delta <= 0:
            if period_start != cycle_start:
                await _persist_overage(sub, cycle_start, billable_units)
            return

        await _persist_overage(sub, cycle_start, billable_units)
        identifier = f"{sub.stripe_id}:{cycle_start.isoformat()}:{reported}"
        try:
            await create_meter_event(
                settings.GLITCHTIP_OVERAGE_METER_EVENT_NAME,
                org.stripe_customer_id,
                delta,
                identifier,
            )
        except StripeError as e:
            logger.exception(
                "Overage meter event failed for org %s (status=%s code=%s)",
                org.id,
                e.status,
                e.code,
            )
            # A 400 (e.g. duplicate identifier: a previous send from this base
            # was recorded) can never succeed on retry — keep the advanced
            # counter, which at worst undercharges. Other statuses are
            # transient: roll back so the next check retries the same delta
            # under the same identifier.
            if e.status != 400:
                await _rollback_overage(sub, cycle_start, billable_units, reported)
        except Exception:
            logger.exception("Failed to report overage meter event for org %s", org.id)
            await _rollback_overage(sub, cycle_start, billable_units, reported)
    finally:
        await cache.adelete(lock_key)


async def _persist_overage(sub, cycle_start, units_reported: int) -> None:
    """Advance the overage counter + cycle anchor for ``sub``, never regressing.

    A stale writer (expired lock) regressing the counter would recompute later
    deltas from too low a base and re-bill units Stripe already recorded, so
    within a cycle the counter only moves forward; a decrease is legitimate
    only when the cycle anchor changes (rollover reset).
    """
    from apps.stripe.models import StripeSubscription

    updated = await StripeSubscription.objects.filter(
        Q(stripe_id=sub.stripe_id)
        & (
            Q(overage_period_start__isnull=True)
            | ~Q(overage_period_start=cycle_start)
            | Q(overage_units_reported__lt=units_reported)
        )
    ).aupdate(overage_units_reported=units_reported, overage_period_start=cycle_start)
    if updated:
        sub.overage_units_reported = units_reported
        sub.overage_period_start = cycle_start


async def _rollback_overage(sub, cycle_start, sent_target: int, base: int) -> None:
    """Best-effort revert of a write-ahead counter after a failed send.

    Conditional on the counter still holding our value, so a concurrent writer
    that advanced further isn't stomped. If the revert itself is lost, the
    stranded delta goes unbilled rather than double-billed.
    """
    from apps.stripe.models import StripeSubscription

    updated = await StripeSubscription.objects.filter(
        stripe_id=sub.stripe_id,
        overage_period_start=cycle_start,
        overage_units_reported=sent_target,
    ).aupdate(overage_units_reported=base)
    if updated:
        sub.overage_units_reported = base


async def _check_and_update_throttle(org: Organization):
    if not settings.BILLING_ENABLED:
        return

    total_event_count = 0

    if org.stripe_primary_subscription:
        from apps.stripe.overage import units_for_budget

        sub = org.stripe_primary_subscription
        price = sub.price
        if price.no_throttle:
            org_throttle = 0
        else:
            plan_events = price.product.events

            period = await get_current_period_dates(org)
            start, end = period if period else (None, None)
            counts = await get_event_counts(org.id, start, end)
            total_event_count = counts.total_event_count

            if org.metered_billing_enabled and sub.metered_item_id and start:
                # Paid overage: charge for usage above quota up to the spend cap,
                # then resume the progressive ramp (no further charges).
                cap_units = units_for_budget(org.overage_spend_cap_cents)
                billable = min(max(0, total_event_count - plan_events), cap_units)
                await _report_overage(org, sub, start, billable)
                org_throttle = _progressive_throttle(
                    total_event_count, plan_events + cap_units, plan_events
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
