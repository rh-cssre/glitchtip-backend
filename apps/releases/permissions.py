from glitchtip.permissions import ScopedPermission


class ReleasePermission(ScopedPermission):
    scope_map = {
        "GET": ["project:read", "project:write", "project:admin", "project:releases"],
        "POST": ["project:write", "project:admin", "project:releases"],
        "PUT": ["project:write", "project:admin", "project:releases"],
        "DELETE": ["project:admin", "project:releases"],
    }

    def get_user_scopes(self, obj, user):
        return obj.organization.get_user_scopes(user)


class ReleaseFilePermission(ReleasePermission):
    def get_user_scopes(self, obj, user):
        return obj.release.organization.get_user_scopes(user)
