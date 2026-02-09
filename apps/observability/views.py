from asgiref.sync import sync_to_async
from django_prometheus import exports

from .metrics import update_metrics


async def prometheus_metrics_view(request):
    await update_metrics()
    return await sync_to_async(exports.ExportToDjangoView)(request)
