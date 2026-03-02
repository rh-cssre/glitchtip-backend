import logging

from django_vtasks import task

logger = logging.getLogger(__name__)


@task
def promote_spans():
    from apps.performance.promotion import promote_spans as _promote

    promoted, truncated = _promote()
    if truncated and promoted > 0:
        # At least one org hit the per-org batch limit — more rows likely remain.
        # Only re-enqueue if progress was made; if all writes failed, the
        # scheduled run (every 5 minutes) will retry without tight-looping.
        logger.info("Promotion batch full (%d rows), re-enqueueing", promoted)
        promote_spans.enqueue()


@task
def compact_span_chunks():
    from apps.performance.promotion import compact_span_chunks as _compact

    _compact()
