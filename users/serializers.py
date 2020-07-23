from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers, exceptions
from dj_rest_auth.serializers import PasswordResetSerializer
from dj_rest_auth.registration.serializers import (
    SocialAccountSerializer as BaseSocialAccountSerializer,
    RegisterSerializer as BaseRegisterSerializer,
)
from allauth.socialaccount.models import SocialApp
from allauth.account.adapter import get_adapter
from allauth.account import app_settings
from allauth.account.utils import filter_users_by_email
from allauth.account.models import EmailAddress
from .utils import is_user_registration_open
from .models import User
from .forms import PasswordSetAndResetForm


class SocialAccountSerializer(BaseSocialAccountSerializer):
    email = serializers.SerializerMethodField()
    username = serializers.SerializerMethodField()

    class Meta(BaseSocialAccountSerializer.Meta):
        fields = (
            "id",
            "provider",
            "uid",
            "last_login",
            "date_joined",
            "email",
            "username",
        )

    def get_email(self, obj):
        if obj.extra_data:
            if "email" in obj.extra_data:
                return obj.extra_data.get("email")
            return obj.extra_data.get("userPrincipalName")  # MS oauth uses this

    def get_username(self, obj):
        if obj.extra_data:
            return obj.extra_data.get("username")


class SocialAppSerializer(serializers.ModelSerializer):
    class Meta:
        model = SocialApp
        fields = ("provider", "name", "client_id")


class ConfirmEmailAddressSerializer(serializers.Serializer):
    email = serializers.EmailField()


class EmailAddressSerializer(serializers.ModelSerializer):
    isPrimary = serializers.BooleanField(source="primary", read_only=True)
    email = serializers.EmailField()  # Remove default unique validation
    isVerified = serializers.BooleanField(source="verified", read_only=True)

    class Meta:
        model = EmailAddress
        fields = ("isPrimary", "email", "isVerified")

    def clean_email(self):
        """ Validate email as done in allauth.account.forms.AddEmailForm """
        value = self.cleaned_data["email"]
        value = get_adapter().clean_email(value)
        errors = {
            "this_account": _(
                "This e-mail address is already associated" " with this account."
            ),
            "different_account": _(
                "This e-mail address is already associated" " with another account."
            ),
        }
        users = filter_users_by_email(value)
        on_this_account = [u for u in users if u.pk == self.user.pk]
        on_diff_account = [u for u in users if u.pk != self.user.pk]

        if on_this_account:
            raise serializers.ValidationError(errors["this_account"])
        if on_diff_account and app_settings.UNIQUE_EMAIL:
            raise serializers.ValidationError(errors["different_account"])
        return value

    def validate(self, data):
        if self.context["request"].method == "POST":
            # Run extra validation on create
            self.user = self.context["request"].user
            self.cleaned_data = data
            data["email"] = self.clean_email()
        return data

    def create(self, validated_data):
        return EmailAddress.objects.add_email(
            self.context["request"], self.user, validated_data["email"], confirm=True
        )

    def update(self, instance, validated_data):
        instance.primary = True
        instance.save()
        return instance


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
            "name",
            "dateJoined",
            "hasPasswordAuth",
            "email",
        )


class UserNotificationsSerializer(serializers.ModelSerializer):
    subscribeByDefault = serializers.BooleanField(source="subscribe_by_default")

    class Meta:
        model = User
        fields = ("subscribeByDefault",)


class RegisterSerializer(BaseRegisterSerializer):
    def validate(self, data):
        if not is_user_registration_open():
            raise exceptions.PermissionDenied("Registration is not open")
        return super().validate(data)


class NoopTokenSerializer(serializers.Serializer):
    """ dj-rest-auth requires tokens, but we don't use them. """


class PasswordSetResetSerializer(PasswordResetSerializer):
    password_reset_form_class = PasswordSetAndResetForm

    def save(self):
        request = self.context.get('request')
        opts = {
            "use_https": request.is_secure(),
            "from_email": getattr(settings, "DEFAULT_FROM_EMAIL"),
            "request": request,
            "subject_template_name": "registration/password_reset_subject.txt",
            "email_template_name": "registration/password_reset_email.txt",
            "html_email_template_name": "registration/password_reset_email.html"
        }

        opts.update(self.get_email_options())
        self.reset_form.save(**opts)
