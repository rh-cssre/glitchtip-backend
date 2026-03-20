import asyncio
import ctypes
import gc
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


def _malloc_trim():
    """Ask glibc to return freed memory to the OS.

    Long-running Python processes accumulate fragmented heap pages that
    glibc's malloc never returns automatically. Calling malloc_trim(0)
    after memory-intensive steps (archival, bulk deletes) releases those
    pages so the worker's RSS stays close to its actual working set.
    """
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _run_step(name: str, func, *args):
    """Run a maintenance step with error isolation and memory cleanup."""
    try:
        func(*args)
    except Exception:
        logger.error("Maintenance step '%s' failed", name, exc_info=True)
    gc.collect()
    _malloc_trim()


@task
def perform_maintenance():
    """
    Update postgres partitions and delete old data.

    Each step is isolated so a failure in one doesn't block the rest.
    gc.collect() + malloc_trim() run after every step to return freed
    memory to the OS, keeping RSS bounded for the next step.
    """
    gc.collect()
    _malloc_trim()
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
