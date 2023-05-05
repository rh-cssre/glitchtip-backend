from django.urls import path

from .views import StatsV2View

urlpatterns = [
    path(
        "organizations/<slug:organization_slug>/stats_v2/",
        StatsV2View.as_view(),
        name="stats-v2",
    ),
]
