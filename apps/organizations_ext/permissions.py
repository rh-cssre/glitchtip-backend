from glitchtip.permissions import ScopedPermission


class OrganizationPermission(ScopedPermission):
    scope_map = {
        "GET": ["org:read", "org:write", "org:admin"],
        "POST": ["org:write", "org:admin"],
        "PUT": ["org:write", "org:admin"],
        "DELETE": ["org:admin"],
    }

    def get_user_scopes(self, obj, user):
        return obj.get_user_scopes(user)


class OrganizationMemberPermission(ScopedPermission):
    scope_map = {
        "GET": ["member:read", "member:write", "member:admin"],
        "POST": ["member:write", "member:admin"],
        "PUT": ["member:write", "member:admin"],
        "DELETE": ["member:admin"],
    }

    def has_permission(self, request, view):
        # teams action has entirely different permissions
        if view.action == "teams":
            permission = OrganizationMemberTeamsPermission()
            if request.auth:
                allowed_scopes = permission.get_allowed_scopes(request, view)
                current_scopes = request.auth.get_scopes()
                return any(s in allowed_scopes for s in current_scopes)
            return bool(request.user and request.user.is_authenticated)
        if view.action == "set_owner":
            if request.auth:
                allowed_scopes = ["org:admin"]
                current_scopes = request.auth.get_scopes()
                return any(s in allowed_scopes for s in current_scopes)
            return bool(request.user and request.user.is_authenticated)
        return super().has_permission(request, view)

    def get_user_scopes(self, obj, user):
        return obj.organization.get_user_scopes(user)


class OrganizationMemberTeamsPermission(OrganizationMemberPermission):
    _allowed_scopes = [
        "org:read",
        "org:write",
        "org:admin",
        "member:read",
        "member:write",
        "member:admin",
    ]
    scope_map = {
        "POST": _allowed_scopes,
        "DELETE": _allowed_scopes,
    }
