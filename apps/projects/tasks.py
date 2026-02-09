from asgiref.sync import sync_to_async
from django.tasks import task

from .models import Project


@task
async def delete_project(project_id: int):
    project = await Project.objects.aget(id=project_id)
    await sync_to_async(project.force_delete)()
