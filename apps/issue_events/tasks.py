from celery import shared_task

from .models import Issue


@shared_task
def delete_issue_task(ids: list[int]):
    for id in ids:
        Issue.objects.get(id=id).force_delete()
