from django.urls import path
from .views import (
    DifsAssembleAPIView, ProjectReprocessingAPIView, DsymsAPIView
)


urlpatterns = [
    path(
        "projects/<slug:organization_slug>/<slug:project_slug>/files/difs/assemble/",  # noqa
        DifsAssembleAPIView.as_view(),
        name="difs-assemble",
    ),
    path(
        "projects/<slug:organization_slug>/<slug:project_slug>/reprocessing/",  # noqa
        ProjectReprocessingAPIView.as_view(),
        name="project-reporcessing"
    ),
    path(
        "projects/<slug:organization_slug>/<slug:project_slug>/files/dsyms/",  # noqa
        DsymsAPIView.as_view(),
        name="dyms"
    )
]
