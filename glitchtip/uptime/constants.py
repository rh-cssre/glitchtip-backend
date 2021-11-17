from django.db import models
from django.utils.translation import gettext_lazy as _


class MonitorType(models.TextChoices):
    PING = "ping", ("Ping")
    GET = "get", ("GET")
    POST = "post", ("POST")
    SSL = "ssl", ("SSL")
    HEARTBEAT = "heartbeat", ("Heartbeat")


class MonitorCheckReason(models.IntegerChoices):
    UNKNOWN = 0, _("Unknown")
    TIMEOUT = 1, _("Timeout")
    STATUS = 2, _("Wrong status code")
    BODY = 3, _("Expected response not found")
    SSL = 4, _("SSL error")
    NETWORK = 5, _("Network error")
