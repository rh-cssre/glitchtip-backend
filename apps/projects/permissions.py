from glitchtip.permissions import ScopedPermission


class ProjectPermission(ScopedPermission):
    scope_map = {
        "GET": ["project:read", "project:write", "project:admin"],
        "POST": ["project:write", "project:admin"],
        "PUT": ["project:write", "project:admin"],
        "DELETE": ["project:admin"],
    }

    def get_user_scopes(self, obj, user):
        return obj.organization.get_user_scopes(user)


class ProjectKeyPermission(ProjectPermission):
    def get_user_scopes(self, obj, user):
        return obj.project.organization.get_user_scopes(user)
