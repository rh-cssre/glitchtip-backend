from django.urls import path
from .views import EventStoreAPIView, MakeSampleErrorView

urlpatterns = [
    path("<int:id>/store/", EventStoreAPIView.as_view(), name="event_store"),
    path("make-sample-error/", MakeSampleErrorView.as_view()),
]
