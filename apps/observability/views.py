import os

import prometheus_client
from asgiref.sync import sync_to_async
from django.http import HttpResponse
from prometheus_client import multiprocess

from .metrics import update_metrics


def _generate_multiproc_metrics():
    """Multiprocess mode: glob + mmap file reads — must run in a thread."""
    registry = prometheus_client.CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return prometheus_client.generate_latest(registry)


async def prometheus_metrics_view(request):
    await update_metrics()
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ or "prometheus_multiproc_dir" in os.environ:
        metrics_page = await sync_to_async(_generate_multiproc_metrics)()
    else:
        # Single process: in-memory registry, no I/O.
        metrics_page = prometheus_client.generate_latest(prometheus_client.REGISTRY)
    return HttpResponse(metrics_page, content_type=prometheus_client.CONTENT_TYPE_LATEST)
