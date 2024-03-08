from django.core.exceptions import SuspiciousOperation
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem
from rest_framework import serializers
from rest_framework.serializers import ModelSerializer

from apps.organizations_ext.models import OrganizationUserRole
from glitchtip.exceptions import ConflictException

from .rest_framework.serializers import (
    SubscriptionSerializer as BaseSubscriptionSerializer,
)


class BaseProductSerializer(ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name", "description", "type", "metadata")


class BasePriceSerializer(ModelSerializer):
    class Meta:
        model = Price
        fields = (
            "id",
            "nickname",
            "currency",
            "type",
            "unit_amount",
            "human_readable_price",
            "metadata",
        )


class PriceSerializer(BasePriceSerializer):
    product = BaseProductSerializer()

    class Meta(BasePriceSerializer.Meta):
        fields = (
            "id",
            "nickname",
            "currency",
            "unit_amount",
            "human_readable_price",
            "metadata",
            "product",
        )


class ProductSerializer(BaseProductSerializer):
    prices = BasePriceSerializer(many=True)

    class Meta(BaseProductSerializer.Meta):
        fields = ("id", "name", "description", "type", "prices", "metadata")


class SubscriptionItemSerializer(ModelSerializer):
    price = PriceSerializer()

    class Meta:
        model = SubscriptionItem
        fields = ("id", "price")


class SubscriptionSerializer(BaseSubscriptionSerializer):
    items = SubscriptionItemSerializer(many=True)


class OrganizationPrimaryKeySerializer(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        user = self.context["request"].user
        return user.organizations_ext_organization.filter(
            organization_users__role=OrganizationUserRole.OWNER
        )


class OrganizationSelectSerializer(serializers.Serializer):
    """Organization in which user is owner of"""

    organization = OrganizationPrimaryKeySerializer()


class PriceForOrganizationSerializer(OrganizationSelectSerializer):
    price = serializers.SlugRelatedField(queryset=Price.objects.all(), slug_field="id")


class CreateSubscriptionSerializer(PriceForOrganizationSerializer):
    """A serializer used to create a Subscription. Only works with free prices."""

    subscription = SubscriptionSerializer(read_only=True)

    def create(self, validated_data):
        organization = validated_data["organization"]
        price = validated_data["price"]
        if price.unit_amount != 0.0:
            raise SuspiciousOperation(
                "Cannot subscribe to non-free plan without payment"
            )
        customer, _ = Customer.get_or_create(subscriber=organization)
        if (
            Subscription.objects.filter(customer=customer)
            .exclude(status="canceled")
            .exists()
        ):
            raise ConflictException("Customer already has subscription")
        subscription = customer.subscribe(items=[{"price": price}])
        return {
            "price": price,
            "organization": organization,
            "subscription": subscription,
        }
