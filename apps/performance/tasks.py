import logging

from asgiref.sync import sync_to_async
from django_vtasks import task

from apps.performance.promotion import compact_span_chunks as _compact_span_chunks
from apps.performance.promotion import promote_spans as _promote_spans

logger = logging.getLogger(__name__)


@task
async def promote_spans():
    promoted, truncated = await _promote_spans()
    if truncated and promoted > 0:
        # At least one org hit the per-org batch limit — more rows likely remain.
        # Only re-enqueue if progress was made; if all writes failed, the
        # scheduled run (every 5 minutes) will retry without tight-looping.
        logger.info("Promotion batch full (%d rows), re-enqueueing", promoted)
        await promote_spans.aenqueue()


@task
async def compact_span_chunks():
    # compact_span_chunks is 100% storage + DuckDB + os work (no Django ORM).
    # Run the entire body on the shared sync executor thread in one hop
    # rather than ping-ponging per call.
    await sync_to_async(_compact_span_chunks)()
