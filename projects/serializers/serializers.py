from rest_framework import serializers
from organizations_ext.serializers.base_serializers import (
    OrganizationReferenceSerializer,
)
from .base_serializers import ProjectReferenceSerializer
from ..models import ProjectKey


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
        return {"public": obj.get_dsn()}


class ProjectSerializer(ProjectReferenceSerializer):
    avatar = serializers.SerializerMethodField()
    color = serializers.SerializerMethodField()
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    features = serializers.SerializerMethodField()
    firstEvent = serializers.SerializerMethodField()
    hasAccess = serializers.SerializerMethodField()
    isBookmarked = serializers.SerializerMethodField()
    isInternal = serializers.SerializerMethodField()
    isPublic = serializers.SerializerMethodField()
    organization = OrganizationReferenceSerializer(read_only=True)

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
            "organization",
            "slug",
            "dateCreated",
            "platform",
        )
        read_only_fields = ("slug", "date_added")

    def get_avatar(self, obj):
        return {"avatarType": "", "avatarUuid": None}

    def get_color(self, obj):
        return ""

    def get_features(self, obj):
        return []

    def get_firstEvent(self, obj):
        return None

    def get_hasAccess(self, obj):
        return True

    def get_isBookmarked(self, obj):
        return False

    def get_isInternal(self, obj):
        return False

    def get_isPublic(self, obj):
        return False
