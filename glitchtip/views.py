import re

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string


async def health(request):
    return HttpResponse("ok", content_type="text/plain")


async def internal_health(request):
    """
    Internal health check in order to be API compatible with sentry servers deployed in self-hosted mode.
    """

    return JsonResponse({"healthy": {}, "problems": []})


def index(request, *args):
    if base_path := settings.FORCE_SCRIPT_NAME:
        content = render_to_string(
            "index.html", {"base_path": base_path}, request=request
        )
        # Replace base href (Not easy to add this as a django template var from angular index.html)
        content = re.sub(r'<base href="/"/>', f'<base href="/{base_path}/">', content)
        return HttpResponse(content)
    return render(request, "index.html")
