from rest_framework import serializers, status
from rest_framework.exceptions import APIException, PermissionDenied
from projects.serializers.base_serializers import ProjectReferenceWithMemberSerializer
from users.serializers import UserSerializer
from teams.serializers import TeamSerializer
from teams.models import Team
from users.models import User
from users.utils import is_user_registration_open
from .base_serializers import OrganizationReferenceSerializer
from ..models import OrganizationUser, OrganizationUserRole, ROLES


class OrganizationSerializer(OrganizationReferenceSerializer):
    pass


class OrganizationDetailSerializer(OrganizationSerializer):
    projects = ProjectReferenceWithMemberSerializer(many=True)
    teams = TeamSerializer(many=True)
    openMembership = serializers.BooleanField(source="open_membership")
    scrubIPAddresses = serializers.BooleanField(source="scrub_ip_addresses")

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + (
            "projects",
            "openMembership",
            "scrubIPAddresses",
            "teams",
        )


class HTTP409APIException(APIException):
    status_code = status.HTTP_409_CONFLICT


class OrganizationUserSerializer(serializers.ModelSerializer):
    user = UserSerializer(required=False, read_only=True)
    role = serializers.CharField(source="get_role")
    roleName = serializers.CharField(source="get_role_display", read_only=True)
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    teams = serializers.SlugRelatedField(
        many=True, write_only=True, slug_field="slug", queryset=Team.objects.none()
    )

    class Meta:
        model = OrganizationUser
        fields = (
            "role",
            "id",
            "user",
            "roleName",
            "dateCreated",
            "email",
            "teams",
            "pending",
        )

    def __init__(self, *args, request_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if "request" in self.context:
            organization_slug = self.context["view"].kwargs.get("organization_slug")
            self.fields["teams"].child_relation.queryset = Team.objects.filter(
                organization__slug=organization_slug
            )

    def get_extra_kwargs(self):
        """email should be read only when updating"""
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
        if (
            not is_user_registration_open()
            and not User.objects.filter(email=email).exists()
        ):
            raise PermissionDenied("Only existing users may be invited")
        if organization.organization_users.filter(email=email).exists():
            raise HTTP409APIException(f"The user {email} is already invited", "email")
        if organization.organization_users.filter(user__email=email).exists():
            raise HTTP409APIException(f"The user {email} is already a member", "email")
        org_user = super().create(
            {"role": role, "email": email, "organization": organization}
        )
        org_user.team_set.add(*teams)
        return org_user

    def update(self, instance, validated_data):
        get_role = validated_data.pop("get_role", None)
        if get_role:
            role = OrganizationUserRole.from_string(get_role)
            validated_data["role"] = role
        return super().update(instance, validated_data)

    def to_representation(self, obj):
        """Override email for representation to potientially show user's email"""
        self.fields["email"] = serializers.SerializerMethodField()
        return super().to_representation(obj)

    def get_email(self, obj):
        """Prefer user primary email over org user email (which is only for invites)"""
        if obj.user:
            return obj.user.email
        return obj.email


class OrganizationUserDetailSerializer(OrganizationUserSerializer):
    teams = serializers.SlugRelatedField(
        source="team_set", slug_field="slug", read_only=True, many=True
    )
    roles = serializers.SerializerMethodField()

    class Meta(OrganizationUserSerializer.Meta):
        fields = OrganizationUserSerializer.Meta.fields + ("roles",)

    def get_roles(self, obj):
        return ROLES


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


class OrganizationUserOrganizationSerializer(OrganizationUserSerializer):
    """Organization User Serializer with Organization info"""

    organization = OrganizationSerializer()

    class Meta(OrganizationUserSerializer.Meta):
        fields = OrganizationUserSerializer.Meta.fields + ("organization",)


class AcceptInviteSerializer(serializers.Serializer):
    accept_invite = serializers.BooleanField()
    org_user = OrganizationUserOrganizationSerializer(read_only=True)
