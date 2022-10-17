from rest_framework.permissions import BasePermission
from users.utils import is_user_registration_open


class ScopedPermission(BasePermission):
    """
    Check if view has scope_map and compare it with request's auth scope map

    Fall back to checking for user authentication
    """

    scope_map = {}

    def get_allowed_scopes(self, request, view):
        try:
            return self.scope_map[request.method]
        except KeyError:
            return {}

    def has_permission(self, request, view):
        if request.auth:
            allowed_scopes = self.get_allowed_scopes(request, view)
            current_scopes = request.auth.get_scopes()
            return any(s in allowed_scopes for s in current_scopes)
        return bool(request.user and request.user.is_authenticated)

    def get_user_scopes(self, obj, user):
        return set()

    def has_object_permission(self, request, view, obj):
        allowed_scopes = self.get_allowed_scopes(request, view)
        current_scopes = self.get_user_scopes(obj, request.user)
        return any(s in allowed_scopes for s in current_scopes)


class UserOnlyPermission(BasePermission):
    """
    Authentication method disallows tokens. User must be logged in via session.
    """

    def has_permission(self, request, view):
        if request.auth:
            return False
        return bool(request.user and request.user.is_authenticated)


class UserRegistrationPermission(BasePermission):
    """
    If registration is closed, only first user can be created except by superuser.
    """

    def has_permission(self, request, view):
        return bool(
            is_user_registration_open() or (request.user and request.user.is_superuser)
        )
