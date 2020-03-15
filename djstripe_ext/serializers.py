from rest_framework.serializers import ModelSerializer
from djstripe.models import Plan
from djstripe.contrib.rest_framework.serializers import (
    SubscriptionSerializer as BaseSubscriptionSerializer,
)


class PlanSerializer(ModelSerializer):
    class Meta:
        model = Plan
        fields = ("id", "nickname", "amount")


class SubscriptionSerializer(BaseSubscriptionSerializer):
    plan = PlanSerializer(read_only=True)
