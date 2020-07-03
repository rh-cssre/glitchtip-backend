from rest_framework import serializers
from ..models import Organization


class OrganizationReferenceSerializer(serializers.ModelSerializer):
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    status = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    isEarlyAdopter = serializers.SerializerMethodField()
    require2FA = serializers.SerializerMethodField()
    isAcceptingEvents = (
        serializers.SerializerMethodField()
    )  # GlitchTip field, not in Sentry OSS

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "dateCreated",
            "status",
            "avatar",
            "isEarlyAdopter",
            "require2FA",
            "isAcceptingEvents",
        )
        read_only_fields = ("id", "slug")

    def get_status(self, obj):
        return {"id": "active", "name": "active"}

    def get_avatar(self, obj):
        return {"avatarType": "", "avatarUuid": None}

    def get_isEarlyAdopter(self, obj):
        return False

    def get_require2FA(self, obj):
        return False

    def get_isAcceptingEvents(self, obj):
        return obj.is_accepting_events

