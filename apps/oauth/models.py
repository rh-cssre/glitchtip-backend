from django.conf import settings
from django.db import models

from apps.api_tokens.models import generate_token
from glitchtip.base_models import CreatedModel


class OAuthApplication(CreatedModel):
    """Dynamically registered OAuth client (RFC 7591)."""

    client_id = models.CharField(max_length=255, unique=True)
    client_secret = models.CharField(max_length=255, blank=True, default="")
    client_id_issued_at = models.IntegerField()
    client_secret_expires_at = models.IntegerField(null=True, blank=True)
    client_info = models.JSONField(
        help_text="Full OAuthClientInformationFull for round-trip fidelity"
    )

    def __str__(self):
        return self.client_id


class OAuthRefreshToken(CreatedModel):
    """Refresh token paired with a cached access token."""

    token = models.CharField(max_length=64, unique=True, default=generate_token)
    application = models.ForeignKey(
        OAuthApplication,
        on_delete=models.CASCADE,
        to_field="client_id",
        related_name="refresh_tokens",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    access_token_key = models.CharField(
        max_length=64,
        help_text="The access token string, for paired revocation",
    )
    scopes = models.TextField(blank=True, help_text="Space-separated scopes")
    expires_at = models.IntegerField(null=True, blank=True)
    is_revoked = models.BooleanField(default=False)

    def __str__(self):
        return self.token
