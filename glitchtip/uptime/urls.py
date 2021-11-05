from django.urls import path

from .views import HeartBeatCheckView

urlpatterns = [
    path(
        "organizations/<slug:organization_slug>/heartbeat_check/<uuid:endpoint_id>/",
        HeartBeatCheckView.as_view(),
        name="heartbeat-check",
    ),
]
