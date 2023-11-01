from django.http import HttpRequest
from django.contrib.auth import SESSION_KEY


class AuthHttpRequest(HttpRequest):
    """Django HttpRequest that is known to be authenticated by a user"""

    auth: str
    "User ID"


def django_auth(request: HttpRequest):
    """
    Check if user is logged in by checking session
    This avoids an unnecessary database call.
    request.auth will result in the user id
    """
    return request.session.get(SESSION_KEY)
