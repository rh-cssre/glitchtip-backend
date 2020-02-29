from rest_framework import viewsets
from .models import Organization, OrganizationUserRole
from .serializers.serializers import (
    OrganizationSerializer,
    OrganizationDetailSerializer,
)


class OrganizationViewSet(viewsets.ModelViewSet):
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    lookup_field = "slug"

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return OrganizationDetailSerializer
        return super().get_serializer_class()

    def perform_create(self, serializer):
        """ Create organization with current user as owner """
        organization = serializer.save()
        organization.add_user(self.request.user, role=OrganizationUserRole.OWNER)
