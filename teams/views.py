from django.shortcuts import get_object_or_404
from rest_framework import viewsets, exceptions
from organizations_ext.models import Organization, OrganizationUserRole
from .serializers import TeamSerializer
from .models import Team


class NestedTeamViewSet(viewsets.ModelViewSet):
    """ Teams for an Organization """

    queryset = Team.objects.all()
    serializer_class = TeamSerializer

    def get_queryset(self):
        return self.queryset.filter(organization__users=self.request.user)

    def perform_create(self, serializer):
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"),
                users=self.request.user,
                organization_users__role__gte=OrganizationUserRole.ADMIN,
            )
        except Organization.DoesNotExist:
            raise exceptions.ValidationError("Organization does not exist")
        team = serializer.save(organization=organization)
        team.members.add(self.request.user)


class TeamViewSet(NestedTeamViewSet):
    lookup_value_regex = r"(?P<organization_slug>[^/.]+)/(?P<team_slug>[-\w]+)"

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        obj = get_object_or_404(
            queryset,
            slug=self.kwargs["team_slug"],
            organization__slug=self.kwargs["organization_slug"],
        )

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj
