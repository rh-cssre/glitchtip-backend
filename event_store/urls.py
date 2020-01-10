from django.urls import path
from .views import EventStoreAPIView, MakeSampleErrorView

urlpatterns = [
    path("<int:id>/store/", EventStoreAPIView.as_view()),
    path("make-sample-error/", MakeSampleErrorView.as_view()),
]
