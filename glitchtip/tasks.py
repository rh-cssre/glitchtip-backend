from celery import shared_task
from django.conf import settings
from django.core.management import call_command

from apps.files.tasks import cleanup_old_files
from apps.performance.tasks import cleanup_old_transaction_events
from apps.uptime.tasks import cleanup_old_monitor_checks
from issues.tasks import cleanup_old_events, reindex_issues_model


@shared_task
def perform_maintenance():
    """
    Update postgres partitions and delete old data
    """
    call_command("pgpartition", yes=True)
    cleanup_old_transaction_events()
    cleanup_old_monitor_checks()
    cleanup_old_files()

    if not settings.GLITCHTIP_ENABLE_NEW_ISSUES:
        cleanup_old_events()
        reindex_issues_model()
