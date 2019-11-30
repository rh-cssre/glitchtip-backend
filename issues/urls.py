from django.urls import path, include, re_path
from rest_framework.routers import DefaultRouter
from .views import IssueViewSet, EventStoreAPIView, MakeSampleErrorView

routeList = ((r"issues", IssueViewSet),)

# The API URLs are now determined automatically by the router.
urlpatterns = [
    path("<int:id>/store/", EventStoreAPIView.as_view()),
    path("make-sample-error/", MakeSampleErrorView.as_view()),
]
