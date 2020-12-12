from django.conf import settings
from django.http import Http404
from rest_framework import viewsets, views, status
from rest_framework.decorators import action
from rest_framework.response import Response
from drf_yasg.utils import swagger_auto_schema
from djstripe.models import Subscription, Customer, Product
from djstripe.settings import STRIPE_SECRET_KEY
import stripe
from issues.models import Event
from .serializers import (
    SubscriptionSerializer,
    CreateSubscriptionSerializer,
    PlanForOrganizationSerializer,
    OrganizationSelectSerializer,
    ProductSerializer,
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
        """ Any user in an org may view subscription data """
        if self.request.user.is_authenticated:
            return self.queryset.filter(
                livemode=settings.STRIPE_LIVE_MODE,
                customer__subscriber__users=self.request.user,
            )
        return self.queryset.none()

    def get_object(self):
        """ Get most recent by slug """
        try:
            return (
                self.get_queryset().filter(**self.kwargs).order_by("-created").first()
            )
        except Subscription.DoesNotExist:
            raise Http404

    @action(detail=True, methods=["get"])
    def events_count(self, *args, **kwargs):
        """ Get event count for current billing period """
        subscription = self.get_object()
        if not subscription:
            return Response(0)
        organization = subscription.customer.subscriber
        event_count = Event.objects.filter(
            issue__project__organization=organization,
            created__gte=subscription.current_period_start,
            created__lt=subscription.current_period_end,
        ).count()
        return Response(event_count)


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Product.objects.filter(
        active=True, livemode=settings.STRIPE_LIVE_MODE, plan__active=True
    ).prefetch_related("plan_set")
    serializer_class = ProductSerializer


class CreateStripeSubscriptionCheckout(views.APIView):
    """ Create Stripe Checkout, send to client for redirecting to Stripe """

    def get_serializer(self, *args, **kwargs):
        return PlanForOrganizationSerializer(
            data=self.request.data, context={"request": self.request}
        )

    @swagger_auto_schema(
        responses={200: str(stripe.api_resources.checkout.session.Session)}
    )
    def post(self, request):
        """ See https://stripe.com/docs/api/checkout/sessions/create """
        serializer = self.get_serializer()
        if serializer.is_valid():
            organization = serializer.validated_data["organization"]
            customer, _ = Customer.get_or_create(subscriber=organization)
            domain = settings.GLITCHTIP_DOMAIN.geturl()
            session = stripe.checkout.Session.create(
                api_key=STRIPE_SECRET_KEY,
                payment_method_types=["card"],
                line_items=[
                    {"price": serializer.validated_data["plan"].id, "quantity": 1,}
                ],
                mode="subscription",
                customer=customer.id,
                success_url=domain
                + "/settings/"
                + organization.slug
                + "/subscription?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=domain + "",
            )

            return Response(session)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class StripeBillingPortal(views.APIView):
    def get_serializer(self, *args, **kwargs):
        return OrganizationSelectSerializer(
            data=self.request.data, context={"request": self.request}
        )

    @swagger_auto_schema(
        responses={200: str(stripe.api_resources.billing_portal.Session)}
    )
    def post(self, request):
        """ See https://stripe.com/docs/billing/subscriptions/integrating-self-serve-portal """
        serializer = self.get_serializer()
        if serializer.is_valid():
            organization = serializer.validated_data["organization"]
            customer, _ = Customer.get_or_create(subscriber=organization)
            domain = settings.GLITCHTIP_DOMAIN.geturl()
            session = stripe.billing_portal.Session.create(
                api_key=STRIPE_SECRET_KEY,
                customer=customer.id,
                return_url=domain + "/settings/" + organization.slug + "/subscription",
            )
            return Response(session)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
