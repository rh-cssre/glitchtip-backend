from rest_framework import viewsets

from .models import Team


class NestedTeamViewSet(viewsets.ReadOnlyModelViewSet):
    """Teams for an Organization"""

    queryset = Team.objects.none()


class TeamViewSet(NestedTeamViewSet):
    lookup_value_regex = r"(?P<organization_slug>[^/.]+)/(?P<team_slug>[-\w]+)"
