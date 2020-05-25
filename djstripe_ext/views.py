from django.conf import settings
from django.http import Http404
from rest_framework import viewsets, views, status
from rest_framework.response import Response
from djstripe.models import Subscription, Plan, Customer
from djstripe.settings import STRIPE_SECRET_KEY
import stripe
from .serializers import (
    SubscriptionSerializer,
    CreateSubscriptionSerializer,
    PlanForOrganizationSerializer,
    OrganizationSerializer,
    PlanSerializer,
)


class SubscriptionViewSet(viewsets.ModelViewSet):
    """
    View subscription status

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
        return self.queryset.filter(
            livemode=settings.STRIPE_LIVE_MODE,
            customer__subscriber__users=self.request.user,
        )

    def get_object(self):
        """ Get most recent by slug """
        try:
            return (
                self.get_queryset().filter(**self.kwargs).order_by("-created").first()
            )
        except Subscription.DoesNotExist:
            raise Http404


class PlanViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Plan.objects.filter(active=True, livemode=settings.STRIPE_LIVE_MODE)
    serializer_class = PlanSerializer


class CreateStripeSubscriptionCheckout(views.APIView):
    """ Create Stripe Checkout, send to client for redirecting to Stripe """

    def post(self, request):
        """ See https://stripe.com/docs/api/checkout/sessions/create """
        serializer = PlanForOrganizationSerializer(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            organization = serializer.validated_data["organization"]
            customer, _ = Customer.get_or_create(subscriber=organization)
            domain = settings.GLITCHTIP_DOMAIN.geturl()
            session = stripe.checkout.Session.create(
                api_key=STRIPE_SECRET_KEY,
                payment_method_types=["card"],
                line_items=[
                    {
                        "price": serializer.validated_data["plan"].id,
                        "quantity": 1,
                        "description": serializer.validated_data["plan"].description,
                    }
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
    def post(self, request):
        """ See https://stripe.com/docs/billing/subscriptions/integrating-self-serve-portal """
        serializer = OrganizationSerializer(
            data=request.data, context={"request": request}
        )
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
