from rest_framework import serializers
from organizations_ext.serializers import OrganizationSerializer
from .models import Project, ProjectKey


class ProjectKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectKey
        fields = (
            "label",
            "public_key",
            "date_added",
            "dsn",
        )
        read_only_fields = ("dsn",)


class ProjectSerializer(serializers.ModelSerializer):
    avatar = serializers.SerializerMethodField()
    color = serializers.SerializerMethodField()
    dateCreated = serializers.DateTimeField(source="date_added", read_only=True)
    features = serializers.SerializerMethodField()
    firstEvent = serializers.SerializerMethodField()
    hasAccess = serializers.SerializerMethodField()
    isBookmarked = serializers.SerializerMethodField()
    isInternal = serializers.SerializerMethodField()
    isMember = serializers.SerializerMethodField()
    isPublic = serializers.SerializerMethodField()
    organization = OrganizationSerializer(read_only=True)

    class Meta:
        model = Project
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

    def get_isMember(self, obj):
        return True

    def get_isPublic(self, obj):
        return False
