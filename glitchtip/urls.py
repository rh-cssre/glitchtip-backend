from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import TemplateView
from django.views.generic.base import RedirectView
from organizations.backends import invitation_backend

from apps.event_ingest.views import event_envelope_view

from .api.api import api
from .views import health, index

urlpatterns = [
    path("_health/", health),
    re_path(
        r"^favicon\.ico$",
        RedirectView.as_view(url=settings.STATIC_URL + "favicon.ico", permanent=True),
    ),
    path(
        "robots.txt",
        TemplateView.as_view(template_name="robots.txt", content_type="text/plain"),
    ),
    path("api/<int:project_id>/envelope/", event_envelope_view),
    path("api/", RedirectView.as_view(url="/profile/auth-tokens")),
    # OSS Sentry compat - redirect the non-api prefix url to the more typical api prefix
    path(
        "organizations/<slug:organization_slug>/issues/<int:issue_id>/events/<event_id>/json/",
        RedirectView.as_view(
            url="/api/0/organizations/%(organization_slug)s/issues/%(issue_id)s/events/%(event_id)s/json/",
        ),
    ),
    path("api/", api.urls),
]

if "django.contrib.admin" in settings.INSTALLED_APPS:
    urlpatterns += [
        path("admin/", admin.site.urls),
    ]

urlpatterns += [
    path("", include("apps.uptime.urls")),
    path("api/test/", include("test_api.urls")),
    path("accounts/", include("allauth.urls")),
    path("_allauth/", include("allauth.headless.urls")),
    # These routes belong to the Angular single page app
    re_path(r"^$", index),
    re_path(
        r"^(auth|login|register|(.*)/issues|(.*)/settings|(.*)/performance|(.*)/projects|(.*)/releases|organizations|profile|(.*)/uptime-monitors|accept|reset-password).*$",
        index,
    ),
    path("accept/", include(invitation_backend().get_urls())),
]

if settings.BILLING_ENABLED:
    urlpatterns.append(path("stripe/", include("djstripe.urls", namespace="djstripe")))

if settings.DEBUG_TOOLBAR:
    urlpatterns.append(path("__debug__/", include("debug_toolbar.urls")))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
