import logging

from asgiref.sync import sync_to_async
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.cache import cache
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
from apps.uptime.models import MonitorCheck, UptimeCheckHourlyStatistic

from .email import InvitationEmail, ThrottleNoticeEmail
from .models import Organization, get_current_period_dates, get_event_counts

logger = logging.getLogger(__name__)


def get_free_tier_cycle(created):
    """
    Calculate the current billing cycle for a free tier organization.
    Anchored to the organization's creation date.
    """
    now = timezone.now()
    if created > now:
        return created, created + relativedelta(months=1)

    # Calculate the number of months between created and now
    # We want the start date to be in the past (or now) and end date in the future
    # created + N months <= now < created + N+1 months

    # Simple approach: set year/month to now, keep day
    # Handle edge cases like Jan 31 -> Feb 28

    # Calculate months difference
    months_diff = (now.year - created.year) * 12 + now.month - created.month

    candidate_start = created + relativedelta(months=months_diff)

    if candidate_start > now:
        months_diff -= 1
        candidate_start = created + relativedelta(months=months_diff)

    return candidate_start, candidate_start + relativedelta(months=1)


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

    org = await (
        Organization.objects.select_related(
            "stripe_primary_subscription__price__product"
        ).aget(id=organization_id)
    )
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


async def _check_and_update_throttle(org: Organization):
    if not settings.BILLING_ENABLED:
        return

    plan_events: int | None = None
    total_event_count = 0

    if org.stripe_primary_subscription:
        price = org.stripe_primary_subscription.price
        if price.no_throttle:
            org_throttle = 0
        else:
            plan_events = price.product.events
            org_throttle = 0

            period = await get_current_period_dates(org)
            start, end = period if period else (None, None)
            counts = await get_event_counts(org.id, start, end)
            total_event_count = counts.total_event_count

            if plan_events is None or total_event_count > plan_events * 2:
                org_throttle = 100
            elif total_event_count > plan_events * 1.5:
                org_throttle = 50
            elif total_event_count > plan_events:
                org_throttle = 10
    else:
        # Free Tier - use anchored cycle dates
        plan_events = settings.GLITCHTIP_FREE_TIER_EVENTS

        start, end = get_free_tier_cycle(org.created)
        counts = await get_event_counts(org.id, start, end)
        total_event_count = counts.total_event_count

        org_throttle = 0
        if total_event_count > plan_events * 2:
            org_throttle = 100
        elif total_event_count > plan_events * 1.5:
            org_throttle = 50
        elif total_event_count > plan_events:
            org_throttle = 10

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
        await sync_to_async(
            model.objects.filter(organization_id=org.id)._raw_delete
        )("default")

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
