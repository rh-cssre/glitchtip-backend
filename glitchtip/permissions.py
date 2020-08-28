from rest_framework.permissions import BasePermission


class ScopedPermission(BasePermission):
    """
    Check if view has scope_map and compare it with request's auth scope map

    Fall back to checking for user authentication
    """

    def get_allowed_scopes(self, request, view):
        return self.scope_map[request.method]

    def has_permission(self, request, view):
        if request.auth:
            allowed_scopes = self.get_allowed_scopes(request, view)
            current_scopes = request.auth.get_scopes()
            return any(s in allowed_scopes for s in current_scopes)
        return bool(request.user and request.user.is_authenticated)

    def get_user_scopes(self, obj, user):
        pass

    def has_object_permission(self, request, view, obj):
        allowed_scopes = self.get_allowed_scopes(request, view)
        current_scopes = self.get_user_scopes(obj, request.user)
        return any(s in allowed_scopes for s in current_scopes)
