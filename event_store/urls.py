from django.urls import path
from django.conf import settings
from .views import EventStoreAPIView, CSPStoreAPIView, test_event_view, EnvelopeAPIView

urlpatterns = [
    path("<int:id>/store/", EventStoreAPIView.as_view(), name="event_store"),
    path("<int:id>/security/", CSPStoreAPIView.as_view(), name="csp_store"),
    path("<int:id>/envelope/", EnvelopeAPIView.as_view(), name="envelope_store"),
]

if settings.DEBUG:
    urlpatterns += [path("event-test/", test_event_view)]
