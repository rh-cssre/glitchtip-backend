from asgiref.sync import sync_to_async
from django.http import HttpRequest
from django.contrib.auth import SESSION_KEY
from django.conf import settings


class AuthHttpRequest(HttpRequest):
    """Django HttpRequest that is known to be authenticated by a user"""

    auth: str
    "User ID"


async def django_auth(request: HttpRequest):
    """
    Check if user is logged in by checking session
    This avoids an unnecessary database call.
    request.auth will result in the user id
    """
    if settings.SESSION_ENGINE == "django.contrib.sessions.backends.cache":
        return request.session.get(SESSION_KEY)
    # Django DB backed sessions don't support async yet
    return await sync_to_async(request.session.get)(SESSION_KEY)
