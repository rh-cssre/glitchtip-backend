from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication

from api_tokens.models import APIToken


class BearerTokenAuthentication(TokenAuthentication):
    """
    Customized TokenAuthentication to support the APIToken model
    and sentry-cli's usage of bearer 
    """

    keyword = "Bearer"
    model = APIToken

    def authenticate_credentials(self, key):
        model = self.get_model()
        try:
            token = model.objects.select_related("user").get(token=key)
        except model.DoesNotExist:
            raise exceptions.AuthenticationFailed(_("Invalid token."))

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed(_("User inactive or deleted."))

        return (token.user, token)
