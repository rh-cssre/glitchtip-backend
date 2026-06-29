from django.urls import path

from .views import status_page_detail

urlpatterns = [
    path(
        "status-pages/<organization>/<slug>/",
        status_page_detail,
        name="status-page-detail",
    ),
]
