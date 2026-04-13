import asyncio
import logging

from django_vtasks import task

logger = logging.getLogger(__name__)


@task
async def promote_spans():
    from apps.performance.promotion import promote_spans as _promote

    promoted, truncated = await asyncio.to_thread(_promote)
    if truncated and promoted > 0:
        # At least one org hit the per-org batch limit — more rows likely remain.
        # Only re-enqueue if progress was made; if all writes failed, the
        # scheduled run (every 5 minutes) will retry without tight-looping.
        logger.info("Promotion batch full (%d rows), re-enqueueing", promoted)
        await promote_spans.aenqueue()


@task
async def compact_span_chunks():
    from apps.performance.promotion import compact_span_chunks as _compact

    await asyncio.to_thread(_compact)
