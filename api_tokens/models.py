import binascii
import os

from django.conf import settings
from django.db import models

from bitfield import BitField


def generate_token():
    return binascii.hexlify(os.urandom(20)).decode()


class APIToken(models.Model):
    """
    Ideas borrowed from rest_framework.authtoken and sentry.apitoken
    """

    token = models.CharField(
        max_length=40, unique=True, editable=False, default=generate_token
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=255, blank=True)
    scopes = BitField(
        flags=(
            "project:read",
            "project:write",
            "project:admin",
            "project:releases",
            "team:read",
            "team:write",
            "team:admin",
            "event:read",
            "event:write",
            "event:admin",
            "org:read",
            "org:write",
            "org:admin",
            "member:read",
            "member:write",
            "member:admin",
        )
    )
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.token
