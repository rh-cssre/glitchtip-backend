from rest_framework import viewsets
from rest_framework.filters import OrderingFilter

from .models import Organization
from .serializers.serializers import (
    OrganizationSerializer,
)


class OrganizationViewSet(viewsets.GenericViewSet):
    filter_backends = [OrderingFilter]
    ordering = ["name"]
    ordering_fields = ["name"]
    queryset = Organization.objects.none()
    serializer_class = OrganizationSerializer
    lookup_field = "slug"
