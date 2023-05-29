from django.urls import include, path
from rest_framework_nested import routers

from .views import (
    CreateStripeSubscriptionCheckout,
    ProductViewSet,
    StripeBillingPortal,
    SubscriptionViewSet,
)

router = routers.SimpleRouter()
router.register(r"subscriptions", SubscriptionViewSet)
router.register(r"products", ProductViewSet)

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
