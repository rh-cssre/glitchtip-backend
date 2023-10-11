from django.conf import settings
from django.urls import path

from .views import (
    # AEventStoreAPIView,
    CSPStoreAPIView,
    EnvelopeAPIView,
    test_event_view,
)

urlpatterns = [
    path("<int:id>/security/", CSPStoreAPIView.as_view(), name="csp_store"),
    path("<int:id>/envelope/", EnvelopeAPIView.as_view(), name="envelope_store"),
]

if settings.DEBUG:
    urlpatterns += [path("event-test/", test_event_view)]
