from glitchtip.permissions import ScopedPermission


class IssuePermission(ScopedPermission):
    scope_map = {
        "GET": ["event:read", "event:write", "event:admin"],
        "POST": ["event:write", "event:admin"],
        "PUT": ["event:write", "event:admin"],
        "DELETE": ["event:admin"],
    }

    def get_user_scopes(self, obj, user):
        return obj.project.organization.get_user_scopes(user)


class EventPermission(IssuePermission):
    def get_user_scopes(self, obj, user):
        return obj.issue.project.organization.get_user_scopes(user)
