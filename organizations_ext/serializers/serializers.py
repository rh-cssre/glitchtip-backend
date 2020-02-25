from organizations.utils import create_organization
from projects.serializers.base_serializers import ProjectReferenceSerializer
from .base_serializers import OrganizationReferenceSerializer


class OrganizationSerializer(OrganizationReferenceSerializer):
    def create(self, validated_data):
        user = self.context["request"].user
        return create_organization(
            user, validated_data["name"], validated_data.get("slug"),
        )


class OrganizationDetailSerializer(OrganizationSerializer):
    projects = ProjectReferenceSerializer(many=True)

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + ("projects",)
