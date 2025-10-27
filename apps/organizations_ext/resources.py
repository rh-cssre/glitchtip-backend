from import_export import resources

from .models import Organization, OrganizationUser


class OrganizationResource(resources.ModelResource):
    class Meta:
        model = Organization
        skip_unchanged = True
        fields = ("id", "slug", "name", "created", "organization")


class OrganizationUserResource(resources.ModelResource):
    class Meta:
        model = OrganizationUser
        skip_unchanged = True
        fields = (
            "id",
            "user",
            "organization",
            "role",
            "email",
        )
        import_id_fields = ("user", "email", "organization")
