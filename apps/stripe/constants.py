from django.db import models


class SubscriptionStatus(models.TextChoices):
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    UNPAID = "unpaid"
    PAUSED = "paused"


ACTIVE_SUBSCRIPTION_STATUSES = (
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.TRIALING,
)


class CollectionMethod(models.TextChoices):
    CHARGE_AUTOMATICALLY = "charge_automatically"
    SEND_INVOICE = "send_invoice"
