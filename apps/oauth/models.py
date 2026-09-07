from django.conf import settings
from django.db import models

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
    """Refresh token paired with a cached access token.

    The refresh token and its paired access token are both stored hashed:
    the plaintext is only ever returned to the client in the token
    exchange response.
    """

    token_prefix = models.CharField(
        max_length=8,
        db_index=True,
        help_text="First 8 chars of the plaintext refresh token, for lookup",
    )
    token_digest = models.CharField(
        max_length=64,
        help_text="SHA-256 hex digest of the plaintext refresh token",
    )
    application = models.ForeignKey(
        OAuthApplication,
        on_delete=models.CASCADE,
        to_field="client_id",
        related_name="refresh_tokens",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    access_token_digest = models.CharField(
        max_length=64,
        help_text="SHA-256 hex digest of the paired access token, for revocation",
    )
    scopes = models.TextField(blank=True, help_text="Space-separated scopes")
    expires_at = models.IntegerField(null=True, blank=True)
    is_revoked = models.BooleanField(default=False)

    def __str__(self):
        return f"OAuthRefreshToken(prefix={self.token_prefix}…)"
