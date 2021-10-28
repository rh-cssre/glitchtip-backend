from releases.permissions import ReleasePermission


class DifsAssemblePermission(ReleasePermission):
    def get_user_scopes(self, obj, user):
        return obj.get_user_scopes(user)


class ProjectReprocessingPermission(ReleasePermission):
    def get_user_scopes(self, obj, user):
        return obj.get_user_scopes(user)


class DymsPermission(ReleasePermission):
    def get_user_scopes(self, obj, user):
        return obj.get_user_scopes(user)
