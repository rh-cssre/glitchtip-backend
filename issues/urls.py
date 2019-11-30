from django.urls import path, include, re_path
from rest_framework.routers import DefaultRouter
from .views import IssueViewSet, EventStoreAPIView

routeList = ((r"issues", IssueViewSet),)

# The API URLs are now determined automatically by the router.
urlpatterns = [
    path("<int:id>/store/", EventStoreAPIView.as_view()),
]
