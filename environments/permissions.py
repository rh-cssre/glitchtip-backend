from organizations_ext.permissions import OrganizationPermission
from projects.permissions import ProjectPermission


class EnvironmentPermission(OrganizationPermission):
    def get_user_scopes(self, obj, user):
        return obj.organization.get_user_scopes(user)


class EnvironmentProjectPermission(ProjectPermission):
    def get_user_scopes(self, obj, user):
        return obj.project.organization.get_user_scopes(user)
