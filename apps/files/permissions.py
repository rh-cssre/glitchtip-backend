from releases.permissions import ReleasePermission


class ChunkUploadPermission(ReleasePermission):
    def get_user_scopes(self, obj, user):
        return obj.get_user_scopes(user)
