from import_export import resources

from .models import Project, ProjectKey


class ProjectKeyResource(resources.ModelResource):
    class Meta:
        model = ProjectKey
        skip_unchanged = True
        fields = ("project", "label", "public_key")
        import_id_fields = (
            "project",
            "public_key",
        )


class ProjectResource(resources.ModelResource):
    class Meta:
        model = Project
        skip_unchanged = True
        fields = ("id", "created", "slug", "name", "organization", "platform")
