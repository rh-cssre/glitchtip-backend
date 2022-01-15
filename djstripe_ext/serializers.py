from django.core.exceptions import SuspiciousOperation
from rest_framework import serializers
from rest_framework.serializers import ModelSerializer
from djstripe.models import Plan, Customer, Product
from organizations_ext.models import OrganizationUserRole
from .rest_framework.serializers import (
    SubscriptionSerializer as BaseSubscriptionSerializer,
)


class BaseProductSerializer(ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name", "description", "type", "metadata")


class PlanSerializer(ModelSerializer):
    product = BaseProductSerializer()

    class Meta:
        model = Plan
        fields = ("id", "nickname", "amount", "metadata", "product")


class ProductSerializer(BaseProductSerializer):
    plans = PlanSerializer(many=True, source="plan_set")

    class Meta(BaseProductSerializer.Meta):
        fields = ("id", "name", "description", "type", "plans", "metadata")


class SubscriptionSerializer(BaseSubscriptionSerializer):
    plan = PlanSerializer(read_only=True)


class OrganizationPrimaryKeySerializer(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        user = self.context["request"].user
        return user.organizations_ext_organization.filter(
            organization_users__role=OrganizationUserRole.OWNER
        )


class OrganizationSelectSerializer(serializers.Serializer):
    """ Organization in which user is owner of """

    organization = OrganizationPrimaryKeySerializer()


class PlanForOrganizationSerializer(OrganizationSelectSerializer):
    plan = serializers.SlugRelatedField(queryset=Plan.objects.all(), slug_field="id")


class CreateSubscriptionSerializer(PlanForOrganizationSerializer):
    """A serializer used to create a Subscription. Only works with free plans. """

    subscription = SubscriptionSerializer(read_only=True)

    def create(self, data):
        organization = data["organization"]
        plan = data["plan"]
        if plan.amount != 0.0:
            raise SuspiciousOperation(
                "Cannot subscribe to non-free plan without payment"
            )
        customer, _ = Customer.get_or_create(subscriber=organization)
        subscription = customer.subscribe(items=[{"plan": plan}])
        return {
            "plan": plan,
            "organization": organization,
            "subscription": subscription,
        }
