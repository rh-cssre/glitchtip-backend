import gc
import logging

from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.tasks import task

from apps.files.maintenance import cleanup_old_files
from apps.issue_events.maintenance import cleanup_old_issue_events, cleanup_old_issues
from apps.logs.maintenance import cleanup_old_logs
from apps.performance.maintenance import cleanup_old_transaction_events
from apps.releases.maintenance import cleanup_old_releases
from apps.sourcecode.maintenance import cleanup_old_debug_symbol_bundles
from apps.stripe.maintenance import sync_stripe_models, update_subscription_cycles
from glitchtip.memory_trim import malloc_trim

logger = logging.getLogger(__name__)


async def _run_step(name: str, coro, *args):
    """Run an async maintenance step with error isolation and memory cleanup."""
    try:
        await coro(*args)
    except Exception:
        logger.error("Maintenance step '%s' failed", name, exc_info=True)
    gc.collect()
    malloc_trim()


@task
async def perform_maintenance():
    """
    Update postgres partitions and delete old data.

    Each step is isolated so a failure in one doesn't block the rest.
    gc.collect() + malloc_trim() run after every step to return freed
    memory to the OS, keeping RSS bounded for the next step.
    """
    gc.collect()
    malloc_trim()
    await _run_step(
        "maintain_partitions", sync_to_async(call_command), "maintain_partitions"
    )
    await _run_step(
        "cleanup_old_transaction_events",
        cleanup_old_transaction_events,
    )
    await _run_step("cleanup_old_files", cleanup_old_files)
    await _run_step("cleanup_old_issue_events", cleanup_old_issue_events)
    await _run_step("cleanup_old_issues", cleanup_old_issues)
    await _run_step(
        "cleanup_old_debug_symbol_bundles",
        cleanup_old_debug_symbol_bundles,
    )
    await _run_step("cleanup_old_releases", cleanup_old_releases)
    await _run_step("cleanup_old_logs", cleanup_old_logs)
    await _run_step("sync_stripe_models", sync_stripe_models)
    await _run_step("update_subscription_cycles", update_subscription_cycles)
