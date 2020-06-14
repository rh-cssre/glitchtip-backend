from rest_framework import serializers
from projects.serializers.base_serializers import ProjectReferenceWithMemberSerializer
from users.serializers import UserSerializer
from .base_serializers import OrganizationReferenceSerializer
from ..models import OrganizationUser


class OrganizationSerializer(OrganizationReferenceSerializer):
    pass


class OrganizationDetailSerializer(OrganizationSerializer):
    projects = ProjectReferenceWithMemberSerializer(many=True)
    openMembership = serializers.BooleanField(source="open_membership")

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + ("projects", "openMembership")


class OrganizationUserSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    role = serializers.SerializerMethodField()
    roleName = serializers.CharField(source="get_role_display")
    dateCreated = serializers.DateTimeField(source="created")
    email = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationUser
        fields = ("role", "id", "user", "roleName", "dateCreated", "email")

    def get_role(self, obj):
        return obj.get_role_display().lower()

    def get_email(self, obj):
        return obj.user.email


class OrganizationUserProjectsSerializer(OrganizationUserSerializer):
    projects = serializers.SerializerMethodField()

    class Meta(OrganizationUserSerializer.Meta):
        fields = OrganizationUserSerializer.Meta.fields + ("projects",)

    def get_projects(self, obj):
        return obj.organization.projects.filter(team__members=obj).values_list(
            "slug", flat=True
        )
