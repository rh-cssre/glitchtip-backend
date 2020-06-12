from rest_framework import serializers
from projects.serializers.base_serializers import ProjectReferenceWithMemberSerializer
from users.serializers import UserSerializer
from .base_serializers import OrganizationReferenceSerializer
from ..models import OrganizationUser


class OrganizationSerializer(OrganizationReferenceSerializer):
    pass


class OrganizationDetailSerializer(OrganizationSerializer):
    projects = ProjectReferenceWithMemberSerializer(many=True)

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + ("projects",)


class OrganizationUserSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    role = serializers.SerializerMethodField()
    roleName = serializers.CharField(source="get_role_display")
    dateCreated = serializers.DateTimeField(source="created")

    class Meta:
        model = OrganizationUser
        fields = ("role", "id", "user", "roleName", "dateCreated")

    def get_role(self, obj):
        return obj.get_role_display().lower()
