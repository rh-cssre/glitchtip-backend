from projects.permissions import ProjectPermission


class ProjectAlertPermission(ProjectPermission):
    scope_map = {
        **ProjectPermission.scope_map,
        "PATCH": ["project:write", "project:admin"],
    }

    def get_user_scopes(self, obj, user):
        return obj.project.organization.get_user_scopes(user)
