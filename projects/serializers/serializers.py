from rest_framework import serializers

from organizations_ext.serializers.base_serializers import (
    OrganizationReferenceSerializer,
)
from teams.serializers import RelatedTeamSerializer

from ..models import ProjectKey
from .base_serializers import ProjectReferenceSerializer


class ProjectKeySerializer(serializers.ModelSerializer):
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    id = serializers.CharField(source="public_key_hex", read_only=True)
    dsn = serializers.SerializerMethodField(read_only=True)
    public = serializers.CharField(source="public_key_hex", read_only=True)
    projectId = serializers.PrimaryKeyRelatedField(source="project", read_only=True)

    class Meta:
        model = ProjectKey
        fields = ("dateCreated", "dsn", "id", "label", "public", "projectId")

    def get_dsn(self, obj):
        return {
            "public": obj.get_dsn(),
            "secret": obj.get_dsn(),  # Deprecated but required for @sentry/wizard
            "security": obj.get_dsn_security(),
        }


class BaseProjectSerializer(ProjectReferenceSerializer):
    avatar = serializers.SerializerMethodField()
    color = serializers.SerializerMethodField()
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    features = serializers.SerializerMethodField()
    firstEvent = serializers.DateTimeField(source="first_event", read_only=True)
    hasAccess = serializers.SerializerMethodField()
    id = serializers.CharField(read_only=True)
    isBookmarked = serializers.SerializerMethodField()
    isInternal = serializers.SerializerMethodField()
    isMember = serializers.SerializerMethodField()
    isPublic = serializers.SerializerMethodField()
    scrubIPAddresses = serializers.BooleanField(
        source="scrub_ip_addresses", required=False
    )
    eventThrottleRate = serializers.IntegerField(
        source="event_throttle_rate",
        min_value=0,
        max_value=100,
        required=False,
    )  # GlitchTip field, not in Sentry OSS

    class Meta(ProjectReferenceSerializer.Meta):
        fields = (
            "avatar",
            "color",
            "features",
            "firstEvent",
            "hasAccess",
            "id",
            "isBookmarked",
            "isInternal",
            "isMember",
            "isPublic",
            "name",
            "scrubIPAddresses",
            "slug",
            "dateCreated",
            "platform",
            "eventThrottleRate",
        )
        read_only_fields = ("slug", "date_added")

    def get_avatar(self, obj):
        return {"avatarType": "", "avatarUuid": None}

    def get_color(self, obj):
        return ""

    def get_features(self, obj):
        return []

    def get_hasAccess(self, obj):
        return True

    def get_isBookmarked(self, obj):
        return False

    def get_isInternal(self, obj):
        return False

    def get_isMember(self, obj):
        user_id = self.context["request"].user.id
        teams = obj.team_set.all()
        # This is actually more performant than:
        # return obj.team_set.filter(members=user).exists()
        for team in teams:
            if user_id in team.members.all().values_list("user_id", flat=True):
                return True
        return False

    def get_isPublic(self, obj):
        return False


class ProjectSerializer(BaseProjectSerializer):
    organization = OrganizationReferenceSerializer(read_only=True)

    class Meta(BaseProjectSerializer.Meta):
        fields = BaseProjectSerializer.Meta.fields + ("organization",)


class ProjectDetailSerializer(ProjectSerializer):
    teams = RelatedTeamSerializer(source="team_set", read_only=True, many=True)

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ("teams",)


class OrganizationProjectSerializer(BaseProjectSerializer):
    teams = RelatedTeamSerializer(source="team_set", read_only=True, many=True)

    class Meta(BaseProjectSerializer.Meta):
        fields = BaseProjectSerializer.Meta.fields + ("teams",)


class ProjectWithKeysSerializer(ProjectSerializer):
    keys = ProjectKeySerializer(many=True, source="projectkey_set")

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ("keys",)
