from .base_serializers import OrganizationReferenceSerializer
from projects.serializers.base_serializers import ProjectReferenceSerializer


class OrganizationSerializer(OrganizationReferenceSerializer):
    pass


class OrganizationDetailSerializer(OrganizationSerializer):
    projects = ProjectReferenceSerializer(many=True)

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + ("projects",)
