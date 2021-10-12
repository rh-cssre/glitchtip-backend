from django.urls import path
from .views import DifsAssembleAPIView, ProjectReprocessingAPIView


urlpatterns = [
    path(
        "projects/<slug:organization_slug>/<slug:project_slug>/files/difs/assemble/", #noqa
        DifsAssembleAPIView.as_view(),
        name="difs-assemble",
    ),
    path(
        "projects/<slug:organization_slug>/<slug:project_slug>/reprocessing/",
        ProjectReprocessingAPIView.as_view(),
        name="project-reporcessing"
    )
]
