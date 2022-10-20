from allauth.account.adapter import DefaultAccountAdapter
from allauth.account import app_settings as allauth_settings
from allauth.account.auth_backends import AuthenticationBackend
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter, get_adapter
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter
from allauth.socialaccount.providers.gitlab.views import GitLabOAuth2Adapter
from allauth.socialaccount.providers.microsoft.views import MicrosoftGraphOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.serializers import (
    SocialLoginSerializer as BaseSocialLoginSerializer,
)
from dj_rest_auth.registration.views import SocialConnectView, SocialLoginView
from django.conf import settings
from django.contrib.auth import get_backends, get_user_model
from django.http import HttpResponseBadRequest
from django.utils.translation import gettext_lazy as _
from django_rest_mfa.helpers import has_mfa
from requests.exceptions import HTTPError
from rest_framework import serializers
from rest_framework.response import Response

from users.utils import is_user_registration_open
from .constants import SOCIAL_ADAPTER_MAP

DOMAIN = settings.GLITCHTIP_URL.geturl()


class MFAAccountAdapter(DefaultAccountAdapter):
    """
    If user requires MFA, do not actually log in
    """

    def login(self, request, user):
        """Extend to check for MFA status, backend hack is copied from super method"""
        if not hasattr(user, "backend"):
            backends = get_backends()
            backend = None
            for b in backends:  # pylint: disable=invalid-name
                if isinstance(b, AuthenticationBackend):
                    # prefer our own backend
                    backend = b
                    break
                elif not backend and hasattr(b, "get_user"):
                    # Pick the first valid one
                    backend = b
            backend_path = ".".join([backend.__module__, backend.__class__.__name__])
            user.backend = backend_path
        if has_mfa(request, user):
            user.mfa = True  # Store for later, to avoid multiple DB checks
        else:
            super().login(request, user)

    def get_login_redirect_url(self, request):
        """Ignore login redirect when not logged in"""
        try:
            return super().get_login_redirect_url(request)
        except AssertionError:
            pass


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return is_user_registration_open()


class SocialLoginSerializer(BaseSocialLoginSerializer):
    tags = serializers.CharField(
        allow_blank=True, required=False, allow_null=True, write_only=True
    )

    # Overriding to add check for django-allauth's is_open_for_signup() at end, to prevent
    # creation of new user on first-time social auth login
    # https://github.com/iMerica/dj-rest-auth/blob/master/dj_rest_auth/registration/serializers.py#L79
    def validate(self, attrs):
        view = self.context.get("view")
        request = self._get_request()

        if not view:
            raise serializers.ValidationError(
                _("View is not defined, pass it as a context variable"),
            )

        adapter_class = getattr(view, "adapter_class", None)
        if not adapter_class:
            raise serializers.ValidationError(_("Define adapter_class in view"))

        adapter = adapter_class(request)
        app = adapter.get_provider().get_app(request)

        access_token = attrs.get("access_token")
        code = attrs.get("code")
        if access_token:
            tokens_to_parse = {"access_token": access_token}
            token = access_token
            id_token = attrs.get("id_token")
            if id_token:
                tokens_to_parse["id_token"] = id_token

        elif code:
            self.set_callback_url(view=view, adapter_class=adapter_class)
            self.client_class = getattr(view, "client_class", None)

            if not self.client_class:
                raise serializers.ValidationError(
                    _("Define client_class in view"),
                )

            provider = adapter.get_provider()
            scope = provider.get_scope(request)
            client = self.client_class(
                request,
                app.client_id,
                app.secret,
                adapter.access_token_method,
                adapter.access_token_url,
                self.callback_url,
                scope,
                scope_delimiter=adapter.scope_delimiter,
                headers=adapter.headers,
                basic_auth=adapter.basic_auth,
            )
            token = client.get_access_token(code)
            access_token = token["access_token"]
            tokens_to_parse = {"access_token": access_token}

            for key in ["refresh_token", "id_token", adapter.expires_in_key]:
                if key in token:
                    tokens_to_parse[key] = token[key]
        else:
            raise serializers.ValidationError(
                _("Incorrect input. access_token or code is required."),
            )

        social_token = adapter.parse_token(tokens_to_parse)
        social_token.app = app

        try:
            login = self.get_social_login(adapter, app, social_token, token)
            ret = complete_social_login(request, login)
        except HTTPError:
            raise serializers.ValidationError(_("Incorrect value"))

        if isinstance(ret, HttpResponseBadRequest):
            raise serializers.ValidationError(ret.content)

        if not login.is_existing:
            if allauth_settings.UNIQUE_EMAIL:
                account_exists = (
                    get_user_model()
                    .objects.filter(
                        email=login.user.email,
                    )
                    .exists()
                )
                if account_exists:
                    raise serializers.ValidationError(
                        _("User is already registered with this e-mail address."),
                    )
            # Added check for open signup
            if not get_adapter(request).is_open_for_signup(request, login):
                raise serializers.ValidationError(_("User registration is closed."))
            else:
                login.lookup()
                login.save(request, connect=True)

        attrs["user"] = login.account.user

        return attrs


class GenericMFAMixin:
    client_class = OAuth2Client  # Needed for Github. Would this ever break a provider?

    @property
    def callback_url(self):
        provider_id = self.adapter_class.provider_id
        return DOMAIN + "/auth/" + provider_id

    @property
    def adapter_class(self):
        provider = self.kwargs.get("provider")
        return SOCIAL_ADAPTER_MAP[provider]


class GlitchTipSocialConnectView(GenericMFAMixin, SocialConnectView):
    pass


class MFASocialLoginView(GenericMFAMixin, SocialLoginView):
    serializer_class = SocialLoginSerializer

    def process_login(self):
        tags = self.serializer.validated_data.get("tags")
        if tags and self.user.analytics is None:
            self.user.set_register_analytics_tags(tags)
            self.user.save(update_fields=["analytics"])

        if not getattr(self.user, "mfa", False):
            super().process_login()

    def get_response(self):
        if getattr(self.user, "mfa", False):
            user_key_types = (
                self.user.userkey_set.all()
                .values_list("key_type", flat=True)
                .distinct()
            )
            return Response({"requires_mfa": True, "valid_auth": user_key_types})
        return super().get_response()
