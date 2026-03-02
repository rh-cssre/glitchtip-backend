import asyncio
import logging

from django.core.management import call_command
from django.tasks import task

from apps.files.maintenance import cleanup_old_files
from apps.issue_events.maintenance import cleanup_old_issue_events, cleanup_old_issues
from apps.logs.maintenance import cleanup_old_logs
from apps.performance.maintenance import cleanup_old_transaction_events
from apps.releases.maintenance import cleanup_old_releases
from apps.sourcecode.maintenance import cleanup_old_debug_symbol_bundles
from apps.stripe.maintenance import sync_stripe_models, update_subscription_cycles

logger = logging.getLogger(__name__)


def _run_step(name: str, func, *args):
    """Run a maintenance step with error isolation."""
    try:
        func(*args)
    except Exception:
        logger.error("Maintenance step '%s' failed", name, exc_info=True)


@task
def perform_maintenance():
    """
    Update postgres partitions and delete old data.

    Each step is isolated so a failure in one doesn't block the rest.
    """
    _run_step("maintain_partitions", call_command, "maintain_partitions")
    _run_step("cleanup_old_transaction_events", cleanup_old_transaction_events)
    _run_step("cleanup_old_files", cleanup_old_files)
    _run_step("cleanup_old_issue_events", cleanup_old_issue_events)
    _run_step("cleanup_old_issues", cleanup_old_issues)
    _run_step("cleanup_old_debug_symbol_bundles", cleanup_old_debug_symbol_bundles)
    _run_step("cleanup_old_releases", cleanup_old_releases)
    _run_step("cleanup_old_logs", cleanup_old_logs)
    _run_step("sync_stripe_models", asyncio.run, sync_stripe_models())
    _run_step("update_subscription_cycles", asyncio.run, update_subscription_cycles())
