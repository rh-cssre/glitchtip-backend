from django.db import models
from django.utils.translation import gettext_lazy as _


class MonitorType(models.TextChoices):
    PING = "ping", _("Ping")
    GET = "get", _("GET")
    POST = "post", _("POST")
    SSL = "ssl", _("SSL")
    HEARTBEAT = "heartbeat", _("Heartbeat")


class MonitorCheckReason(models.IntegerChoices):
    UNKNOWN = 0, _("Unknown")
    TIMEOUT = 1, _("Timeout")
    STATUS = 2, _("Wrong status code")
    BODY = 3, _("Expected response not found")
    SSL = 4, _("SSL error")
    NETWORK = 5, _("Network error")

