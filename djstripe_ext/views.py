import stripe
from django.conf import settings
from django.core.cache import cache
from django.db.models import Prefetch
from django.http import Http404
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem
from djstripe.settings import djstripe_settings
from rest_framework import status, views, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from organizations_ext.models import Organization

from .serializers import (
    CreateSubscriptionSerializer,
    OrganizationSelectSerializer,
    PriceForOrganizationSerializer,
    ProductSerializer,
    SubscriptionSerializer,
)


class SubscriptionViewSet(viewsets.ModelViewSet):
    """
    View subscription status and create new free tier subscriptions

    Use organization slug for detail view. Ex: /subscriptions/my-cool-org/
    """

    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer
    lookup_field = "customer__subscriber__slug"

    def get_serializer_class(self):
        if self.action == "create":
            return CreateSubscriptionSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        """Any user in an org may view subscription data"""
        if self.request.user.is_authenticated:
            return self.queryset.filter(
                livemode=settings.STRIPE_LIVE_MODE,
                customer__subscriber__users=self.request.user,
            ).prefetch_related(
                Prefetch(
                    "items",
                    queryset=SubscriptionItem.objects.select_related("price__product"),
                )
            )
        return self.queryset.none()

    def get_object(self):
        """Get most recent by slug"""
        try:
            subscription = (
                self.get_queryset()
                .filter(**self.kwargs)
                .exclude(status="canceled")
                .order_by("-created")
                .first()
            )
            # Check organization throttle, in case it changed recently
            if subscription:
                Organization.objects.filter(
                    id=subscription.customer.subscriber_id,
                    is_accepting_events=False,
                    is_active=True,
                    djstripe_customers__subscriptions__plan__amount__gt=0,
                    djstripe_customers__subscriptions__status="active",
                ).update(is_accepting_events=True)

            return subscription
        except Subscription.DoesNotExist as exc:
            raise Http404 from exc

    @action(detail=True, methods=["get"])
    def events_count(self, *args, **kwargs):
        """Get event count for current billing period"""
        subscription = self.get_object()
        if not subscription:
            return Response(
                {
                    "eventCount": 0,
                    "transactionEventCount": 0,
                    "uptimeCheckEventCount": 0,
                    "fileSizeMB": 0,
                }
            )
        organization = subscription.customer.subscriber
        org = Organization.objects.with_event_counts().get(pk=organization.pk)
        data = {
            "eventCount": org.issue_event_count,
            "transactionEventCount": org.transaction_count,
            "uptimeCheckEventCount": org.uptime_check_event_count,
            "fileSizeMB": org.file_size,
        }
        return Response(data)


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Stripe Product + Prices

    unit_amount is price in cents
    """

    queryset = (
        Product.objects.filter(
            active=True,
            livemode=settings.STRIPE_LIVE_MODE,
            prices__active=True,
            metadata__events__isnull=False,
            metadata__is_public="true",
        )
        .prefetch_related(
            Prefetch("prices", queryset=Price.objects.filter(active=True))
        )
        .distinct()
    )
    serializer_class = ProductSerializer


class CreateStripeSubscriptionCheckout(views.APIView):
    """Create Stripe Checkout, send to client for redirecting to Stripe"""

    def get_serializer(self, *args, **kwargs):
        return PriceForOrganizationSerializer(
            data=self.request.data, context={"request": self.request}
        )

    def post(self, request):
        """See https://stripe.com/docs/api/checkout/sessions/create"""
        serializer = self.get_serializer()
        if serializer.is_valid():
            organization = serializer.validated_data["organization"]
            customer, _ = Customer.get_or_create(subscriber=organization)
            domain = settings.GLITCHTIP_URL.geturl()
            session = stripe.checkout.Session.create(
                api_key=djstripe_settings.STRIPE_SECRET_KEY,
                payment_method_types=["card"],
                line_items=[
                    {
                        "price": serializer.validated_data["price"].id,
                        "quantity": 1,
                    }
                ],
                mode="subscription",
                customer=customer.id,
                automatic_tax={
                    "enabled": settings.STRIPE_AUTOMATIC_TAX,
                },
                customer_update={"address": "auto", "name": "auto"},
                tax_id_collection={
                    "enabled": True,
                },
                success_url=domain
                + "/"
                + organization.slug
                + "/settings/subscription?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=domain + "",
            )

            return Response(session)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class StripeBillingPortal(views.APIView):
    def get_serializer(self, *args, **kwargs):
        return OrganizationSelectSerializer(
            data=self.request.data, context={"request": self.request}
        )

    def post(self, request):
        """See https://stripe.com/docs/billing/subscriptions/integrating-self-serve-portal"""
        serializer = self.get_serializer()
        if serializer.is_valid():
            organization = serializer.validated_data["organization"]
            customer, _ = Customer.get_or_create(subscriber=organization)
            domain = settings.GLITCHTIP_URL.geturl()
            session = stripe.billing_portal.Session.create(
                api_key=djstripe_settings.STRIPE_SECRET_KEY,
                customer=customer.id,
                return_url=domain + "/" + organization.slug + "/settings/subscription",
            )
            return Response(session)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
