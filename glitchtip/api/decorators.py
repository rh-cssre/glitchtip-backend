"""API route decorators."""


def optional_slash(router, method: str, path: str, **kwargs):
    """
    Register a route for both /path/ and /path variants.

    Some Sentry-compatible tools (particularly iOS-related) may send requests
    with or without trailing slashes. When a URL doesn't match any route,
    Django's CSRF middleware runs before returning 404, causing confusing
    CSRF errors instead of proper JSON responses.

    This decorator registers both URL variants:
    - With trailing slash (canonical, included in OpenAPI schema)
    - Without trailing slash (hidden from schema)

    Usage:
        @optional_slash(router, "post", "files/difs/assemble/")
        async def difs_assemble_api(...):
            ...
    """
    path_with_slash = path.rstrip("/") + "/"
    path_without_slash = path.rstrip("/")

    def decorator(func):
        # Canonical path (with slash) - included in OpenAPI schema
        getattr(router, method)(path_with_slash, **kwargs)(func)
        # Alternate path (without slash) - hidden from schema
        getattr(router, method)(path_without_slash, include_in_schema=False, **kwargs)(
            func
        )
        return func

    return decorator
