from django.conf import settings
from pydantic import HttpUrl, field_validator

from glitchtip.schema import CamelSchema
from glitchtip.url_validation import validate_public_url


class ImportIn(CamelSchema):
    url: HttpUrl
    auth_token: str
    organization_slug: str

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: HttpUrl) -> HttpUrl:
        # The import source is a remote GlitchTip server supplied by an org
        # admin; reject internal targets so it can't be used for SSRF. Operators
        # migrating from an internal instance opt in via GLITCHTIP_ALLOW_PRIVATE_IPS.
        try:
            validate_public_url(
                str(v), allow_private=settings.GLITCHTIP_ALLOW_PRIVATE_IPS
            )
        except ValueError as err:
            raise ValueError(str(err)) from err
        return v
