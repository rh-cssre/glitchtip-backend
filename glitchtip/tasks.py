import asyncio

from django.core.management import call_command
from django.tasks import task

from apps.files.tasks import cleanup_old_files
from apps.issue_events.maintenance import cleanup_old_issues
from apps.performance.maintenance import cleanup_old_transaction_events
from apps.sourcecode.maintenance import cleanup_old_debug_symbol_bundles
from apps.stripe.maintenance import sync_stripe_models


@task
def perform_maintenance():
    """
    Update postgres partitions and delete old data
    """
    call_command("maintain_partitions")
    cleanup_old_transaction_events()
    cleanup_old_files()
    cleanup_old_issues()
    cleanup_old_debug_symbol_bundles()
    asyncio.run(sync_stripe_models())
