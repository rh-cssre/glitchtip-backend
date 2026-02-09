from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.internal.flows.login import record_authentication
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from apps.users.utils import (
    is_social_apps_user_registration_open,
    is_user_registration_open,
)
from glitchtip.email import GlitchTipEmail


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return is_social_apps_user_registration_open()


class CustomDefaultAccountAdapter(DefaultAccountAdapter):
    def render_mail(self, template_prefix, email, context, headers=None):
        headers = headers or {}
        default_headers = GlitchTipEmail.get_default_headers()
        for key, value in default_headers.items():
            if key not in headers:
                headers[key] = value
        return super().render_mail(template_prefix, email, context, headers)

    def is_open_for_signup(self, request):
        return is_user_registration_open()

    def save_user(self, request, user, form, commit=True):
        # Consider a signup a form of authentication
        user = super().save_user(request, user, form, commit)
        if commit:
            record_authentication(request, user, method="signup")
        return user
