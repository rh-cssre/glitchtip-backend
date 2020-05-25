from django.urls import path, include
from rest_framework_nested import routers
from .views import (
    SubscriptionViewSet,
    PlanViewSet,
    CreateStripeSubscriptionCheckout,
    StripeBillingPortal,
)

router = routers.SimpleRouter()
router.register(r"subscriptions", SubscriptionViewSet)
router.register(r"plans", PlanViewSet)

urlpatterns = [
    path("", include(router.urls)),
    path(
        "create-stripe-subscription-checkout/",
        CreateStripeSubscriptionCheckout.as_view(),
        name="create-stripe-subscription-checkout",
    ),
    path(
        "create-billing-portal/",
        StripeBillingPortal.as_view(),
        name="create-billing-portal",
    ),
]
