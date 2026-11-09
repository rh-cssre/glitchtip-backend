from django.db.models import F, Q
from django.tasks import task

from apps.organizations_ext.models import Organization

from .constants import EventStatus
from .maintenance import delete_issues_in_batches
from .models import Issue, IssueEvent, IssueHash, IssueIndex
from .services import IssueFilters, filter_issue_list, get_queryset

CHUNK_SIZE = 1000


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
    event_status = EventStatus.from_string(status) if status else None
    merge_id = update_params.get("merge")
    issue_update = {
        key: update_params[key]
        for key in (
            "assigned_to_org_user_id",
            "assigned_to_team_id",
            "resolved_in_release_id",
        )
        if key in update_params
    }

    if event_status is not None or issue_update:
        # Select each batch once and apply every change to it. A pass per change
        # would re-run the filter after the status change, and a filter such as
        # is:unresolved would then match nothing.
        last_issue_id = 0
        while True:
            batch_ids = [
                i
                async for i in remaining_qs.filter(id__gt=last_issue_id)
                .order_by("id")
                .values_list("id", flat=True)[:CHUNK_SIZE]
            ]
            if not batch_ids:
                break
            last_issue_id = batch_ids[-1]

            if event_status is not None:
                # status lives on the IssueIndex leaf
                await (
                    IssueIndex.objects.filter(
                        issue_id__in=batch_ids, organization_id=organization_id
                    )
                    .exclude(status=event_status)
                    .aupdate(status=event_status)
                )
            if issue_update:
                await Issue.objects.filter(id__in=batch_ids).aupdate(**issue_update)

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
                .values_list("id", flat=True)[:CHUNK_SIZE]
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
                    ).values_list("id", flat=True)[:CHUNK_SIZE]
                ]
                if not event_ids:
                    await Issue.objects.filter(id__in=batch_ids).aupdate(
                        is_deleted=True
                    )
                    break

                moved_event_count = await IssueEvent.objects.filter(
                    id__in=event_ids,
                    issue_id__in=batch_ids,
                    organization_id=organization_id,
                ).aupdate(issue=target_issue)
                # The target is outside batch_ids, so moved events must leave the source selection.
                if not moved_event_count:
                    break
                await IssueIndex.objects.filter(
                    issue_id=target_issue.id, organization_id=organization_id
                ).aupdate(count=F("count") + moved_event_count)
