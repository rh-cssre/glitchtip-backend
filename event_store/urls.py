from django.urls import path
from .views import EventStoreAPIView, CSPStoreAPIView

urlpatterns = [
    path("<int:id>/store/", EventStoreAPIView.as_view(), name="event_store"),
    path("<int:id>/security/", CSPStoreAPIView.as_view(), name="csp_store"),
]
