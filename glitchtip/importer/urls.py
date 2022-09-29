from django.urls import path
from .views import ImportAPIView


urlpatterns = [path("import", ImportAPIView.as_view(), name="import")]
