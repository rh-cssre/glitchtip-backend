from rest_framework import viewsets
from .models import Organization, OrganizationUserRole, OrganizationUser
from .serializers.serializers import (
    OrganizationSerializer,
    OrganizationDetailSerializer,
    OrganizationUserSerializer,
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


class OrganizationMemberViewSet(viewsets.ReadOnlyModelViewSet):
    """ All Organization Users including pending """

    queryset = OrganizationUser.objects.all()
    serializer_class = OrganizationUserSerializer

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        return queryset


class OrganizationUserViewSet(OrganizationMemberViewSet):
    """ Organization users - excluding pending invites """

    # def get_queryset(self):
    # TODO add pending filter
