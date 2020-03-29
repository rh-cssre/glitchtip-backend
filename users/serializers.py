from rest_auth.serializers import UserDetailsSerializer as BaseUserDetailsSerializer
from rest_auth.registration.serializers import SocialAccountSerializer


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
