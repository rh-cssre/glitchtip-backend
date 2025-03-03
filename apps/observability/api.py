from django_prometheus import exports
from ninja import Router
from ninja.errors import HttpError

from apps.users.models import User
from glitchtip.api.authentication import AuthHttpRequest

from .metrics import compile_metrics

router = Router()


@router.get("observability/django/")
async def django_prometheus_metrics(request: AuthHttpRequest):
    if not await User.objects.filter(id=request.auth.user_id, is_staff=True).aexists():
        raise HttpError(403, "is_staff required")
    await compile_metrics()
    return exports.ExportToDjangoView(request)
