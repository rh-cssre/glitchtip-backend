from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from django.shortcuts import get_object_or_404
from organizations.backends import invitation_backend
from rest_framework import exceptions, permissions, status, views, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from apps.organizations_ext.utils import is_organization_creation_open
from apps.teams.serializers import TeamSerializer

from .invitation_backend import InvitationTokenGenerator
from .models import Organization, OrganizationUser, OrganizationUserRole
from .permissions import (
    OrganizationMemberPermission,
    OrganizationMemberTeamsPermission,
    OrganizationPermission,
)
from .serializers.serializers import (
    AcceptInviteSerializer,
    OrganizationDetailSerializer,
    OrganizationSerializer,
    OrganizationUserDetailSerializer,
    OrganizationUserProjectsSerializer,
    OrganizationUserSerializer,
    ReinviteSerializer,
)


class OrganizationViewSet(viewsets.ModelViewSet):
    filter_backends = [OrderingFilter]
    ordering = ["name"]
    ordering_fields = ["name"]
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    lookup_field = "slug"
    permission_classes = [OrganizationPermission]

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return OrganizationDetailSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        queryset = self.queryset.filter(users=self.request.user)

        if self.action in ["retrieve"]:
            queryset = queryset.prefetch_related(
                "projects__team_set__members",
                "teams__members",
            )
        return queryset

    def perform_create(self, serializer):
        """
        Create organization with current user as owner
        If registration is closed, only superusers can create new orgs.
        """
        if not is_organization_creation_open() and not self.request.user.is_superuser:
            raise exceptions.PermissionDenied("Organization creation is not open")
        organization = serializer.save()
        organization.add_user(self.request.user, role=OrganizationUserRole.OWNER)


class OrganizationMemberViewSet(viewsets.ModelViewSet):
    """
    API compatible with undocumented Sentry endpoint `/api/organizations/<slug>/members/`
    """

    queryset = OrganizationUser.objects.all()
    serializer_class = OrganizationUserSerializer
    permission_classes = [OrganizationMemberPermission]

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return OrganizationUserDetailSerializer
        return super().get_serializer_class()

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
            "user__socialaccount_set", "organization__owner"
        )

    def get_object(self):
        pk = self.kwargs.get("pk")
        if pk == "me":
            obj = get_object_or_404(self.get_queryset(), user=self.request.user)
            self.check_object_permissions(self.request, obj)
            return obj
        return super().get_object()

    def check_permissions(self, request):
        if self.request.user.is_authenticated and self.action in [
            "create",
            "update",
            "partial_update",
            "destroy",
        ]:
            org_slug = self.kwargs.get("organization_slug")
            try:
                user_org_user = (
                    self.request.user.organizations_ext_organizationuser.get(
                        organization__slug=org_slug
                    )
                )
            except ObjectDoesNotExist:
                raise PermissionDenied("Not a member of this organization")
            if user_org_user.role < OrganizationUserRole.MANAGER:
                raise PermissionDenied(
                    "Must be manager or higher to add/remove organization members"
                )
        return super().check_permissions(request)

    def update(self, request, *args, **kwargs):
        """
        Update can both reinvite a user or change the org user which require different request data
        However it always returns OrganizationUserSerializer regardless

        Updates are always partial. Only teams and role may be edited.
        """
        if self.action in ["update"] and self.request.data.get("reinvite"):
            return self.reinvite(request)
        kwargs["partial"] = True
        return super().update(request, *args, **kwargs)

    def reinvite(self, request):
        """
        Send additional invitation to user
        This works more like a rest action, but is embedded within the update view for compatibility
        """
        instance = self.get_object()
        serializer = ReinviteSerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        invitation_backend().send_invitation(instance)
        serializer = self.serializer_class(instance)
        return Response(serializer.data)

    def perform_create(self, serializer):
        try:
            organization = self.request.user.organizations_ext_organization.get(
                slug=self.kwargs.get("organization_slug")
            )
        except ObjectDoesNotExist:
            raise Http404

        org_user = serializer.save(organization=organization)
        invitation_backend().send_invitation(org_user)
        return org_user

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if hasattr(instance, "organizationowner"):
            return Response(
                data={
                    "message": "User is organization owner. Transfer ownership first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def check_team_member_permission(self, org_user, user, team):
        """Check if user has permission to update team members"""
        open_membership = org_user.organization.open_membership
        is_self = org_user.user == user

        if open_membership and is_self:
            return  # Ok to modify yourself in any way with open_membership

        in_team = team.members.filter(user=user).exists()
        if in_team:
            required_role = OrganizationUserRole.ADMIN
        else:
            required_role = OrganizationUserRole.MANAGER

        if not self.request.user.organizations_ext_organizationuser.filter(
            organization=org_user.organization, role__gte=required_role
        ).exists():
            raise exceptions.PermissionDenied("Must be admin to modify teams")

    @action(detail=True, methods=["post"])
    def set_owner(self, request, *args, **kwargs):
        """
        Set this team member as the one and only one Organization owner
        Only an existing Owner or user with the "org:admin" scope is able to perform this.
        """
        new_owner = self.get_object()
        organization = new_owner.organization
        user = request.user
        if not (
            organization.is_owner(user)
            or organization.organization_users.filter(
                user=user, role=OrganizationUserRole.OWNER
            ).exists()
        ):
            raise exceptions.PermissionDenied("Only owner may set organization owner.")
        organization.change_owner(new_owner)
        return self.retrieve(request, *args, **kwargs)

    @action(
        detail=True,
        methods=["post", "delete"],
        url_path=r"teams/(?P<members_team_slug>[-\w]+)",
    )
    def teams(self, request, pk=None, organization_slug=None, members_team_slug=None):
        """Add existing organization user to a team"""
        if not pk or not organization_slug or not members_team_slug:
            raise exceptions.MethodNotAllowed(request.method)

        pk = self.kwargs.get("pk")
        if pk == "me":
            org_user = get_object_or_404(self.get_queryset(), user=self.request.user)
        else:
            queryset = self.filter_queryset(self.get_queryset())
            lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
            filter_kwargs = {self.lookup_field: self.kwargs[lookup_url_kwarg]}
            org_user = get_object_or_404(queryset, **filter_kwargs)

        team = org_user.organization.teams.filter(slug=members_team_slug).first()

        # Instead of check_object_permissions
        permission = OrganizationMemberTeamsPermission()
        if not permission.has_permission(request, self):
            self.permission_denied(
                request, message=getattr(permission, "message", None)
            )
        self.check_team_member_permission(org_user, self.request.user, team)

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


class AcceptInviteView(views.APIView):
    """Accept invite to organization"""

    serializer_class = AcceptInviteSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def validate_token(self, org_user, token):
        if not InvitationTokenGenerator().check_token(org_user, token):
            raise exceptions.PermissionDenied("Invalid invite token")

    def get(self, request, org_user_id=None, token=None):
        org_user = get_object_or_404(OrganizationUser, pk=org_user_id)
        self.validate_token(org_user, token)
        serializer = self.serializer_class(
            {"accept_invite": False, "org_user": org_user}
        )
        return Response(serializer.data)

    def post(self, request, org_user_id=None, token=None):
        org_user = get_object_or_404(OrganizationUser, pk=org_user_id)
        self.validate_token(org_user, token)
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["accept_invite"]:
            org_user.accept_invite(request.user)
        serializer = self.serializer_class(
            {
                "accept_invite": serializer.validated_data["accept_invite"],
                "org_user": org_user,
            }
        )
        return Response(serializer.data)
