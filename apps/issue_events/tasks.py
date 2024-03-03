from celery import shared_task

from .models import Issue


@shared_task
def delete_issue_task(id: int):
    Issue.objects.get(id=id).force_delete()
