from django.urls import path
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_cookie

from .views import HeartBeatCheckView, StatusPageDetailView

urlpatterns = [
    path(
        "api/0/organizations/<slug:organization_slug>/heartbeat_check/<uuid:endpoint_id>/",
        HeartBeatCheckView.as_view(),
        name="heartbeat-check",
    ),
    path(
        "status-pages/<organization>/<slug>/",
        cache_page(60)(vary_on_cookie(StatusPageDetailView.as_view())),
        name="status-page-detail",
    ),
]
