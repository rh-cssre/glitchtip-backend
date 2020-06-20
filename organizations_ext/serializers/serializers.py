from rest_framework import serializers
from projects.serializers.base_serializers import ProjectReferenceWithMemberSerializer
from users.serializers import UserSerializer
from users.models import User
from teams.serializers import TeamSerializer
from teams.models import Team
from .base_serializers import OrganizationReferenceSerializer
from ..models import OrganizationUser, OrganizationUserRole


class OrganizationSerializer(OrganizationReferenceSerializer):
    pass


class OrganizationDetailSerializer(OrganizationSerializer):
    projects = ProjectReferenceWithMemberSerializer(many=True)
    teams = TeamSerializer(many=True)
    openMembership = serializers.BooleanField(source="open_membership")

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + (
            "projects",
            "openMembership",
            "teams",
        )


class OrganizationUserSerializer(serializers.ModelSerializer):
    user = UserSerializer(required=False)
    role = serializers.CharField(source="get_role")
    roleName = serializers.CharField(source="get_role_display", read_only=True)
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    teams = serializers.SlugRelatedField(
        many=True, write_only=True, slug_field="slug", queryset=Team.objects.none()
    )

    class Meta:
        model = OrganizationUser
        fields = ("role", "id", "user", "roleName", "dateCreated", "email", "teams")

    def get_extra_kwargs(self):
        """ email should be read only when updating """
        extra_kwargs = super().get_extra_kwargs()
        if self.instance is not None:
            extra_kwargs["email"] = {"read_only": True}
            extra_kwargs["user"] = {"read_only": True}

        return extra_kwargs

    def create(self, validated_data):
        role = OrganizationUserRole.from_string(validated_data.get("get_role"))
        email = validated_data.get("email")
        organization = validated_data.get("organization")
        teams = validated_data.get("teams")
        user = User.objects.filter(
            emailaddress__email=email, emailaddress__verified=True
        ).first()
        return super().create(
            {"role": role, "user": user, "email": email, "organization": organization}
        )

    def update(self, instance, validated_data):
        get_role = validated_data.pop("get_role", None)
        if get_role:
            role = OrganizationUserRole.from_string(get_role)
            validated_data["role"] = role
        return super().update(instance, validated_data)


class OrganizationUserProjectsSerializer(OrganizationUserSerializer):
    projects = serializers.SerializerMethodField()

    class Meta(OrganizationUserSerializer.Meta):
        fields = OrganizationUserSerializer.Meta.fields + ("projects",)

    def get_projects(self, obj):
        return obj.organization.projects.filter(team__members=obj).values_list(
            "slug", flat=True
        )


class ReinviteSerializer(serializers.Serializer):
    reinvite = serializers.IntegerField()

    def update(self, instance, validated_data):
        if validated_data.get("reinvite"):
            pass
            # Send email
        return instance

