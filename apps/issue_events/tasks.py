from django.db.models import F, Q
from django.tasks import task

from apps.organizations_ext.models import Organization

from .constants import EventStatus
from .maintenance import delete_issues_in_batches
from .models import Issue, IssueEvent, IssueHash, IssueIndex
from .services import IssueFilters, filter_issue_list, get_queryset

UPDATE_ISSUE_CHUNK_SIZE = 1000
MERGE_ISSUE_CHUNK_SIZE = 1000
MERGE_EVENT_CHUNK_SIZE = 1000


@task
async def delete_issue_task(ids: list[int]):
    # delete_issues_in_batches handles partitioned FK tables (IssueEvent,
    # IssueAggregate, IssueTag) and non-partitioned dependents per batch.
    await delete_issues_in_batches(Issue.objects.filter(id__in=ids))


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
    qs = filter_issue_list(qs, filters).filter(id__lte=max_id)
    remaining_qs = qs.exclude(id__in=exclude_ids)

    # IssueIndex partition key, so the leaf writes below prune to one
    # hash partition instead of scanning all of them.
    organization_id = (
        await Organization.objects.filter(slug=organization_slug)
        .values_list("id", flat=True)
        .afirst()
    )

    status = update_params.get("status")
    merge_id = update_params.get("merge")
    assignee_org_user_id = update_params.get("assigned_to_org_user_id")
    assignee_team_id = update_params.get("assigned_to_team_id")
    apply_assignment = "assigned_to_org_user_id" in update_params

    if status:
        event_status = EventStatus.from_string(status)
        # Optimization: Don't update rows that already match. status lives on the
        # IssueIndex leaf.
        status_qs = remaining_qs.exclude(index__status=event_status)

        last_issue_id = 0
        while True:
            batch_ids = [
                i
                async for i in status_qs.filter(id__gt=last_issue_id)
                .order_by("id")
                .values_list("id", flat=True)[:UPDATE_ISSUE_CHUNK_SIZE]
            ]
            if not batch_ids:
                break
            last_issue_id = batch_ids[-1]

            await IssueIndex.objects.filter(
                issue_id__in=batch_ids, organization_id=organization_id
            ).aupdate(status=event_status)

    if apply_assignment:
        last_issue_id = 0
        while True:
            batch_ids = [
                i
                async for i in remaining_qs.filter(id__gt=last_issue_id)
                .order_by("id")
                .values_list("id", flat=True)[:UPDATE_ISSUE_CHUNK_SIZE]
            ]
            if not batch_ids:
                break
            last_issue_id = batch_ids[-1]
            await Issue.objects.filter(id__in=batch_ids).aupdate(
                assigned_to_org_user_id=assignee_org_user_id,
                assigned_to_team_id=assignee_team_id,
            )

    if merge_id:
        target_issue = await qs.filter(id=merge_id, is_deleted=False).afirst()
        if not target_issue:
            return

        merge_qs = qs.filter(
            Q(is_deleted=False) | Q(id__in=exclude_ids),
            project_id=target_issue.project_id,
        ).exclude(id=target_issue.id)

        last_issue_id = 0
        while True:
            batch_ids = [
                i
                async for i in merge_qs.filter(id__gt=last_issue_id)
                .order_by("id")
                .values_list("id", flat=True)[:MERGE_ISSUE_CHUNK_SIZE]
            ]
            if not batch_ids:
                break

            last_issue_id = batch_ids[-1]

            await IssueHash.objects.filter(issue_id__in=batch_ids).aupdate(
                issue=target_issue
            )

            while True:
                event_ids = [
                    event_id
                    async for event_id in IssueEvent.objects.filter(
                        issue_id__in=batch_ids,
                        organization_id=organization_id,
                    ).values_list("id", flat=True)[:MERGE_EVENT_CHUNK_SIZE]
                ]
                if not event_ids:
                    break

                moved_event_count = await IssueEvent.objects.filter(
                    id__in=event_ids,
                    issue_id__in=batch_ids,
                    organization_id=organization_id,
                ).aupdate(issue=target_issue)
                if moved_event_count:
                    await IssueIndex.objects.filter(
                        issue_id=target_issue.id, organization_id=organization_id
                    ).aupdate(count=F("count") + moved_event_count)

            await Issue.objects.filter(id__in=batch_ids).aupdate(is_deleted=True)
