from django.db import models
from django.utils.translation import gettext_lazy as _


class MonitorType(models.TextChoices):
    PING = "Ping"
    GET = "GET"
    POST = "POST"
    PORT = "TCP Port"
    SSL = "SSL"
    HEARTBEAT = "Heartbeat"


HTTP_MONITOR_TYPES = (MonitorType.PING, MonitorType.GET, MonitorType.POST)

# Fallback ceiling on monitors per organization, applied only on billing-enabled
# installs whose plan sets no `uptime_monitors` Stripe product metadata value.
# High enough that a real user should never meet it; a plan that needs more says
# so in Stripe.
DEFAULT_MAX_MONITORS_PER_ORGANIZATION = 100


class MonitorCheckReason(models.IntegerChoices):
    UNKNOWN = 0, _("Unknown")
    TIMEOUT = 1, _("Timeout")
    STATUS = 2, _("Wrong status code")
    BODY = 3, _("Expected response not found")
    SSL = 4, _("SSL error")
    NETWORK = 5, _("Network error")
