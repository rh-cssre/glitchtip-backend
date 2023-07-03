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


class MonitorCheckReason(models.IntegerChoices):
    UNKNOWN = 0, _("Unknown")
    TIMEOUT = 1, _("Timeout")
    STATUS = 2, _("Wrong status code")
    BODY = 3, _("Expected response not found")
    SSL = 4, _("SSL error")
    NETWORK = 5, _("Network error")
