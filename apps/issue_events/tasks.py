from django.db.models import F
from django.tasks import task

from .constants import EventStatus
from .models import Issue, IssueEvent, IssueHash
from .services import IssueFilters, filter_issue_list, get_queryset


@task
def delete_issue_task(ids: list[int]):
    for id in ids:
        Issue.objects.get(id=id).force_delete()


@task
async def update_issues_task(
    organization_slug: str,
    user_id: int,
    filter_params: dict,
    update_params: dict,
    exclude_ids: list[int],
    max_id: int,
):
    """
    Background task to update issues in chunks.
    """
    filters = IssueFilters(**filter_params)
    qs = await get_queryset(user_id, organization_slug=organization_slug)
    qs = filter_issue_list(qs, filters)
    qs = qs.exclude(id__in=exclude_ids).filter(id__lte=max_id)

    status = update_params.get("status")
    merge_id = update_params.get("merge")

    if status:
        event_status = EventStatus.from_string(status)
        # Optimization: Don't update rows that already match
        qs = qs.exclude(status=event_status)

        chunk_size = 1000
        while True:
            # Fetch IDs to lock minimally
            batch_ids = [i async for i in qs.values_list("id", flat=True)[:chunk_size]]
            if not batch_ids:
                break

            await Issue.objects.filter(id__in=batch_ids).aupdate(status=event_status)

    if merge_id:
        try:
            target_issue = await Issue.objects.aget(id=merge_id)
        except Issue.DoesNotExist:
            return

        updated_issue_count = 0
        chunk_size = 1000
        while True:
            batch_ids = [
                i
                async for i in qs.exclude(id=target_issue.id).values_list(
                    "id", flat=True
                )[:chunk_size]
            ]
            if not batch_ids:
                target_issue.count = F("count") + updated_issue_count
                await target_issue.asave(update_fields=["count"])
                break

            # Soft delete source issues
            await Issue.objects.filter(id__in=batch_ids).aupdate(is_deleted=True)

            # Move Hashes
            await IssueHash.objects.filter(issue_id__in=batch_ids).aupdate(
                issue=target_issue
            )

            # Caution: Moving millions of events is heavy.
            # Switch only the first 1000 events
            event_ids = []
            async for event_id in IssueEvent.objects.filter(
                issue_id__in=batch_ids
            ).values_list("id", flat=True)[:1000]:
                event_ids.append(event_id)

            if event_ids:
                await IssueEvent.objects.filter(id__in=event_ids).aupdate(
                    issue=target_issue
                )
                updated_issue_count += len(event_ids)
