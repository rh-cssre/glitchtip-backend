from celery import shared_task
from django.core.management import call_command

from apps.files.tasks import cleanup_old_files
from apps.issue_events.maintenance import cleanup_old_issues
from apps.performance.maintenance import cleanup_old_transaction_events


@shared_task
def perform_maintenance():
    """
    Update postgres partitions and delete old data
    """
    call_command("pgpartition", yes=True)
    cleanup_old_transaction_events()
    cleanup_old_files()
    cleanup_old_issues()
