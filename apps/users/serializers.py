import hashlib
import hmac

from allauth.account import app_settings
from allauth.account.adapter import get_adapter
from allauth.account.forms import default_token_generator
from allauth.account.models import EmailAddress
from allauth.account.utils import filter_users_by_email
from allauth.socialaccount.models import SocialApp
from allauth.socialaccount.providers.openid_connect.views import OpenIDConnectAdapter
from dj_rest_auth.registration.serializers import (
    RegisterSerializer as BaseRegisterSerializer,
)
from dj_rest_auth.registration.serializers import (
    SocialAccountSerializer as BaseSocialAccountSerializer,
)
from dj_rest_auth.serializers import PasswordResetSerializer
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from glitchtip.constants import SOCIAL_ADAPTER_MAP

from .forms import PasswordSetAndResetForm
from .models import User


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
    authorize_url = serializers.SerializerMethodField()
    scopes = serializers.SerializerMethodField()
    provider = serializers.SerializerMethodField()

    class Meta:
        model = SocialApp
        fields = ("provider", "name", "client_id", "authorize_url", "scopes")

    def get_authorize_url(self, obj):
        request = self.context.get("request")
        adapter_cls = SOCIAL_ADAPTER_MAP.get(obj.provider)
        if adapter_cls == OpenIDConnectAdapter:
            adapter = adapter_cls(request, obj.provider_id)
        else:
            adapter = adapter_cls(request)
        if adapter:
            return adapter.authorize_url

    def get_scopes(self, obj):
        request = self.context.get("request")
        if request:
            provider = obj.get_provider(request)
            return provider.get_scope(request)

    def get_provider(self, obj):
        return obj.provider_id or obj.provider


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
        """Validate email as done in allauth.account.forms.AddEmailForm"""
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

    def validate(self, attrs):
        if self.context["request"].method == "POST":
            # Run extra validation on create
            self.user = self.context["request"].user
            self.cleaned_data = attrs
            attrs["email"] = self.clean_email()
        return attrs

    def create(self, validated_data):
        return EmailAddress.objects.add_email(
            self.context["request"], self.user, validated_data["email"], confirm=True
        )

    def update(self, instance, validated_data):
        instance.primary = True
        instance.save()
        return instance


class UserSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="email", read_only=True)
    lastLogin = serializers.DateTimeField(source="last_login", read_only=True)
    isSuperuser = serializers.BooleanField(source="is_superuser")
    emails = EmailAddressSerializer(many=True, default=[])
    identities = SocialAccountSerializer(
        source="socialaccount_set", many=True, read_only=True
    )
    id = serializers.CharField()
    isActive = serializers.BooleanField(source="is_active")
    dateJoined = serializers.DateTimeField(source="created", read_only=True)
    hasPasswordAuth = serializers.BooleanField(
        source="has_usable_password", read_only=True
    )

    class Meta:
        model = User
        fields = (
            "username",
            "lastLogin",
            "isSuperuser",
            "emails",
            "identities",
            "id",
            "isActive",
            "name",
            "dateJoined",
            "hasPasswordAuth",
            "email",
            "options",
        )


class CurrentUserSerializer(UserSerializer):
    chatwootIdentifierHash = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ("chatwootIdentifierHash",)

    def get_chatwootIdentifierHash(self, obj):
        if settings.CHATWOOT_WEBSITE_TOKEN and settings.CHATWOOT_IDENTITY_TOKEN:
            secret = bytes(settings.CHATWOOT_IDENTITY_TOKEN, "utf-8")
            message = bytes(str(obj.id), "utf-8")

            hash = hmac.new(secret, message, hashlib.sha256)
            return hash.hexdigest()


class RegisterSerializer(BaseRegisterSerializer):
    tags = serializers.CharField(
        write_only=True,
        allow_blank=True,
        required=False,
        help_text="Additional UTM (analytics) data",
    )

    def custom_signup(self, request, user):
        tags = self.validated_data.get("tags")
        if tags:
            user.set_register_analytics_tags(tags)
            user.save(update_fields=["analytics"])


class UserNotificationsSerializer(serializers.ModelSerializer):
    subscribeByDefault = serializers.BooleanField(source="subscribe_by_default")

    class Meta:
        model = User
        fields = ("subscribeByDefault",)


class NoopTokenSerializer(serializers.Serializer):
    """dj-rest-auth requires tokens, but we don't use them."""


class PasswordSetResetSerializer(PasswordResetSerializer):
    password_reset_form_class = PasswordSetAndResetForm

    def save(self):
        request = self.context.get("request")
        opts = {
            "use_https": request.is_secure(),
            "from_email": getattr(settings, "DEFAULT_FROM_EMAIL"),
            "request": request,
            "token_generator": default_token_generator,
            "subject_template_name": "registration/password_reset_subject.txt",
            "email_template_name": "registration/password_reset_email.txt",
            "html_email_template_name": "registration/password_reset_email.html",
        }

        opts.update(self.get_email_options())
        self.reset_form.save(**opts)
