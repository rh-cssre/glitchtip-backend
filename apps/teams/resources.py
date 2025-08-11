from import_export import resources

from .models import Team


class TeamResource(resources.ModelResource):
    class Meta:
        model = Team
        skip_unchanged = True
        fields = ("id", "slug", "created", "organization", "projects", "members")
