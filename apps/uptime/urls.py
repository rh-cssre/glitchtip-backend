from django.urls import path

from .views import StatusPageDetailView

urlpatterns = [
    path(
        "status-pages/<organization>/<slug>/",
        StatusPageDetailView.as_view(),
        name="status-page-detail",
    ),
]
