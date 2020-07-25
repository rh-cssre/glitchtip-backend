from django.urls import path
from .views import ErrorPageEmbedView

urlpatterns = [
    path("error-page/", ErrorPageEmbedView.as_view(), name="error_page"),
]
