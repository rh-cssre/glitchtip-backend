from django.urls import path, include
from rest_framework_nested import routers
from .views import SubscriptionViewSet

router = routers.SimpleRouter()
router.register(r"subscriptions", SubscriptionViewSet)

urlpatterns = [path("", include(router.urls))]
