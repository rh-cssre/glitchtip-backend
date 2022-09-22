from django.shortcuts import get_object_or_404
from rest_framework import viewsets, exceptions
from organizations_ext.models import Organization, OrganizationUserRole
from .serializers import TeamSerializer
from .models import Team
from .permissions import TeamPermission


class NestedTeamViewSet(viewsets.ModelViewSet):
    """ Teams for an Organization """

    queryset = Team.objects.all()
    serializer_class = TeamSerializer
    permission_classes = [TeamPermission]

    def get_queryset(self):
        if self.request.user.is_authenticated:
            queryset = self.queryset.filter(organization__users=self.request.user)
            organization_slug = self.kwargs.get("organization_slug")
            if organization_slug:
                queryset = queryset.filter(organization__slug=organization_slug)
            return queryset.prefetch_related("members", "projects")
        return self.queryset.none()

    def perform_create(self, serializer):
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"),
                users=self.request.user,
                organization_users__role__gte=OrganizationUserRole.ADMIN,
            )
        except Organization.DoesNotExist as org_no_exist:
            raise exceptions.ValidationError(
                "Organization does not exist"
            ) from org_no_exist
        if Team.objects.filter(
            organization=organization, slug=serializer.validated_data.get("slug")
        ).exists():
            raise exceptions.ValidationError("Slug must be unique for organization")
        team = serializer.save(organization=organization)
        org_user = organization.organization_users.filter(
            user=self.request.user
        ).first()
        team.members.add(org_user)


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
