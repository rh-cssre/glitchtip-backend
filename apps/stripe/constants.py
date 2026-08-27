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

# Statuses a subscription can never come back from. The rest of the inactive
# set (unpaid, paused, incomplete) is recoverable: the customer pays the open
# invoice or resumes, and the same row returns to active carrying its items.
# Discarding opt-in state on those would silently strip a setting the customer
# paid for and chose.
TERMINAL_SUBSCRIPTION_STATUSES = (
    SubscriptionStatus.CANCELED,
    SubscriptionStatus.INCOMPLETE_EXPIRED,
)


class CollectionMethod(models.TextChoices):
    CHARGE_AUTOMATICALLY = "charge_automatically"
    SEND_INVOICE = "send_invoice"
