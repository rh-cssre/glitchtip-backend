from rest_framework import serializers
from dj_rest_auth.serializers import UserDetailsSerializer as BaseUserDetailsSerializer
from dj_rest_auth.registration.serializers import SocialAccountSerializer
from allauth.account.models import EmailAddress
from .models import User


class EmailSerializer(serializers.ModelSerializer):
    isPrimary = serializers.BooleanField(source="primary", read_only=True)
    isVerified = serializers.BooleanField(source="verified", read_only=True)

    class Meta:
        model = EmailAddress
        fields = ("isPrimary", "email", "isVerified")


class UserSerializer(serializers.ModelSerializer):
    lastLogin = serializers.DateTimeField(source="last_login", read_only=True)
    isSuperuser = serializers.BooleanField(source="is_superuser")
    identities = SocialAccountSerializer(
        source="socialaccount_set", many=True, read_only=True
    )
    isActive = serializers.BooleanField(source="is_active")
    dateJoined = serializers.DateTimeField(source="created", read_only=True)
    hasPasswordAuth = serializers.BooleanField(
        source="has_usable_password", read_only=True
    )

    class Meta:
        model = User
        fields = (
            "lastLogin",
            "isSuperuser",
            "identities",
            "id",
            "isActive",
            "dateJoined",
            "hasPasswordAuth",
            "email",
        )


class UserNotificationsSerializer(serializers.ModelSerializer):
    subscribeByDefault = serializers.BooleanField(source="subscribe_by_default")

    class Meta:
        model = User
        fields = ("subscribeByDefault",)


class UserDetailsSerializer(BaseUserDetailsSerializer):
    """ Extended UserDetailsSerializer with social account set data """

    socialaccount_set = SocialAccountSerializer(many=True, read_only=True)

    class Meta(BaseUserDetailsSerializer.Meta):
        fields = (
            "pk",
            "email",
            "first_name",
            "last_name",
            "socialaccount_set",
        )
        read_only_fields = ("email",)
