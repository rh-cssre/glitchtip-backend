from glitchtip.permissions import ScopedPermission


class TeamPermission(ScopedPermission):
    scope_map = {
        "GET": ["team:read", "team:write", "team:admin"],
        "POST": ["team:write", "team:admin"],
        "PUT": ["team:write", "team:admin"],
        "DELETE": ["team:admin"],
    }

    def get_user_scopes(self, obj, user):
        return obj.organization.get_user_scopes(user)
