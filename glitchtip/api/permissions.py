from functools import wraps

from ninja.errors import HttpError

from .authentication import AuthHttpRequest


def has_permission(permissions: list[str]):
    """
    Check scoped permissions. At this time only token authentication is checked.

    Example: @has_permission(["event:write", "event:admin"])

    The decorated function requires at least one of the specified permissions.
    """

    def decorator(f):
        @wraps(f)
        async def decorated_function(request: AuthHttpRequest, *args, **kwargs):
            if request.auth.auth_type == "token":
                scopes = request.auth.data.get_scopes()
                if not any(s in permissions for s in scopes):
                    raise HttpError(403, "Permission denied")
            return await f(request, *args, **kwargs)

        return decorated_function

    return decorator
