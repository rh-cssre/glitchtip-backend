from rest_framework import serializers
from organizations.models import Organization


class OrganizationSerializer(serializers.ModelSerializer):
    agreeTerms = serializers.BooleanField(write_only=True, required=True)
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    status = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    isEarlyAdopter = serializers.SerializerMethodField()
    require2FA = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "dateCreated",
            "agreeTerms",
            "status",
            "avatar",
            "isEarlyAdopter",
            "require2FA",
        )
        read_only_fields = ("id",)

    def create(self, validated_data):
        validated_data.pop("agreeTerms")
        return super().create(validated_data)

    def get_status(self, obj):
        return {"id": "active", "name": "active"}

    def get_avatar(self, obj):
        return {"avatarType": "", "avatarUuid": None}

    def get_isEarlyAdopter(self, obj):
        return False

    def get_require2FA(self, obj):
        return False
