from dataclasses import dataclass
from typing import Any, Literal, Optional

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from ninja.security import HttpBearer
from ninja.security import SessionAuth as BaseSessionAuth

from apps.api_tokens.models import APIToken


@dataclass
class Auth:
    user_id: int
    auth_type: Literal["session", "token"]
    data: Any = None


class AuthHttpRequest(HttpRequest):
    """Django HttpRequest that is known to be authenticated by a user"""

    auth: Auth


class SessionAuth(BaseSessionAuth):
    """
    Check if user is logged in by checking session
    This avoids an unnecessary database call.
    request.auth will result in the user id
    """

    async def authenticate(
        self, request: HttpRequest, key: Optional[str]
    ) -> Optional[Auth]:
        if settings.SESSION_ENGINE == "django.contrib.sessions.backends.cache":
            user_id = request.session.get(SESSION_KEY)
        # Django DB backed sessions don't support async yet
        else:
            user_id = await sync_to_async(request.session.get)(SESSION_KEY)
        return Auth(int(user_id), "session") if user_id else None


class TokenAuth(HttpBearer):
    """
    API Token based authentication always connects to a specific user.
    Store the token object under data for checking scopes permissions.
    """

    def __call__(self, request: HttpRequest) -> Optional[Any]:
        # Workaround https://github.com/vitalik/django-ninja/issues/989
        if request.resolver_match and request.resolver_match.url_name in [
            "setup_wizard_hash"
        ]:
            request.auth = None  # type: ignore
            return self.get_anon()

        return super().__call__(request)

    async def get_anon(self):
        return AnonymousUser()

    async def authenticate(self, request: HttpRequest, key: str) -> Optional[Auth]:
        try:
            token = await APIToken.objects.aget(token=key, user__is_active=True)
        except APIToken.DoesNotExist:
            return None

        return Auth(token.user_id, "token", data=token)
