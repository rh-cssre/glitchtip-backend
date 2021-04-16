from django.urls import path
from .views import ChunkUploadAPIView


urlpatterns = [
    path(
        "organizations/<slug:organization_slug>/chunk-upload/",
        ChunkUploadAPIView.as_view(),
        name="chunk-upload",
    ),
]
