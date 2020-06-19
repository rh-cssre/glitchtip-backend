from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from rest_framework import viewsets, exceptions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from teams.serializers import TeamSerializer
from .models import Organization, OrganizationUserRole, OrganizationUser
from .serializers.serializers import (
    OrganizationSerializer,
    OrganizationDetailSerializer,
    OrganizationUserSerializer,
    OrganizationUserProjectsSerializer,
)


class OrganizationViewSet(viewsets.ModelViewSet):
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    lookup_field = "slug"

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return OrganizationDetailSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        return self.queryset.filter(users=self.request.user).prefetch_related(
            "projects__team_set__members",
        )

    def perform_create(self, serializer):
        """ Create organization with current user as owner """
        organization = serializer.save()
        organization.add_user(self.request.user, role=OrganizationUserRole.OWNER)


class OrganizationMemberViewSet(viewsets.ModelViewSet):
    """
    API compatible with undocumented Sentry endpoint `/api/organizations/<slug>/members/`
    """

    queryset = OrganizationUser.objects.all()
    serializer_class = OrganizationUserSerializer

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        team_slug = self.kwargs.get("team_slug")
        if team_slug:
            queryset = queryset.filter(team__slug=team_slug)
        return queryset.select_related("organization", "user").prefetch_related(
            "user__socialaccount_set"
        )

    def perform_create(self, serializer):
        try:
            organization = self.request.user.organizations_ext_organization.get(
                slug=self.kwargs.get("organization_slug")
            )
        except ObjectDoesNotExist:
            raise Http404

        user_org_user = self.request.user.organizations_ext_organizationuser.get(
            organization=organization
        )
        if user_org_user.role < OrganizationUserRole.MANAGER:
            raise PermissionDenied(
                "User must be manager or higher to add organization members"
            )
        return serializer.save(organization=organization)

    def has_team_member_permission(self, org_user, user, team) -> bool:
        open_membership = org_user.organization.open_membership
        is_self = org_user.user == user

        if open_membership and is_self:
            return True  # Ok to modify yourself in any way with open_membership

        in_team = team.members.filter(user=user).exists()
        if in_team:
            required_role = OrganizationUserRole.ADMIN
        else:
            required_role = OrganizationUserRole.MANAGER

        if not self.request.user.organizations_ext_organizationuser.filter(
            organization=org_user.organization, role__gte=required_role
        ).exists():
            return False
        return True

    @action(
        detail=True,
        methods=["post", "delete"],
        url_path=r"teams/(?P<members_team_slug>[-\w]+)",
    )
    def teams(self, request, pk=None, organization_slug=None, members_team_slug=None):
        """ Add existing organization user to a team """
        if not pk or not organization_slug or not members_team_slug:
            raise exceptions.MethodNotAllowed(request.method)

        org_user = self.get_object()
        team = org_user.organization.teams.filter(slug=members_team_slug).first()

        if not self.has_team_member_permission(org_user, self.request.user, team):
            raise exceptions.PermissionDenied("Must be admin to modify teams")

        if not team:
            raise exceptions.NotFound()

        if request.method == "POST":
            team.members.add(org_user)
            serializer = TeamSerializer(team, context={"request": request})
            return Response(serializer.data, status=201)
        elif request.method == "DELETE":
            team.members.remove(org_user)
            serializer = TeamSerializer(team, context={"request": request})
            return Response(serializer.data, status=200)


class OrganizationUserViewSet(OrganizationMemberViewSet):
    """
    Extension of OrganizationMemberViewSet that adds projects the user has access to

    API compatible with [get-organization-users](https://docs.sentry.io/api/organizations/get-organization-users/)
    """

    serializer_class = OrganizationUserProjectsSerializer
