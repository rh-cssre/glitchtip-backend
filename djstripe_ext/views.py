from django.conf import settings
from django.http import Http404
from rest_framework import viewsets
from djstripe.models import Subscription
from .serializers import SubscriptionSerializer


class SubscriptionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    View subscription status

    Use organization slug for detail view. Ex: /subscriptions/my-cool-org/
    """

    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer
    lookup_field = "customer__subscriber__slug"

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
