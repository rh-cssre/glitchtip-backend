from django.db import models


class SubscriptionQuotaWarning(models.Model):
    """Track when quota warnings are sent out"""

    subscription = models.OneToOneField(
        "djstripe.Subscription", on_delete=models.CASCADE
    )
    notice_last_sent = models.DateTimeField(auto_now=True)
