from celery import shared_task

from .models import Project


@shared_task
def delete_project(project_id: int):
    Project.objects.get(id=project_id).force_delete()
