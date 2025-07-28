from import_export import resources

from .models import User


class UserResource(resources.ModelResource):
    class Meta:
        model = User
        skip_unchanged = True
        fields = (
            "id",
            # "password",
            "is_superuser",
            "email",
            "name",
            "is_staff",
            "is_active",
            "created",
        )
