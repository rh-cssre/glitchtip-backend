import binascii
import os
from typing import List

from django.conf import settings
from django.db import models

from bitfield import BitField
from glitchtip.base_models import CreatedModel


def generate_token():
    return binascii.hexlify(os.urandom(32)).decode()


class APIToken(CreatedModel):
    """
    Ideas borrowed from rest_framework.authtoken and sentry.apitoken
    """

    token = models.CharField(
        max_length=64, unique=True, editable=False, default=generate_token
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

    def __str__(self):
        return self.token

    def get_scopes(self):
        """
        Return array of set scope flags.
        Example: ["project:read"]
        """
        return [i[0] for i in self.scopes.items() if i[1] is True]

    def add_permission(self, permission: str):
        """Add permission flag to scopes and save"""
        setattr(self.scopes, permission, True)
        self.save(update_fields=["scopes"])

    def add_permissions(self, permissions: List[str]):
        """Add permission flags to scopes and save"""
        for permission in permissions:
            setattr(self.scopes, permission, True)
        self.save(update_fields=["scopes"])
