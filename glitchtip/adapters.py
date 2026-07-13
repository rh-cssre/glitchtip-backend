import aiohttp
from allauth.account.internal.flows.login import record_authentication
from allauth.socialaccount import app_settings as socialaccount_app_settings
from allauth_async.account.adapter import AsyncDefaultAccountAdapter
from allauth_async.account.internal.flows.login import arecord_authentication
from allauth_async.socialaccount.adapter import AsyncDefaultSocialAccountAdapter
from django.conf import settings

from apps.users.utils import (
    ais_social_apps_user_registration_open,
    ais_user_registration_open,
    is_social_apps_user_registration_open,
    is_user_registration_open,
)
from glitchtip.email import GlitchTipEmail


class CustomSocialAccountAdapter(AsyncDefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, _sociallogin):
        return is_social_apps_user_registration_open()

    async def ais_open_for_signup(self, request, _sociallogin):
        return await ais_social_apps_user_registration_open()

    def open_http_session(self):
        # Match the outbound-HTTP behavior the sync stack got from `requests`
        # (which trusts proxy env vars by default) and stamp our User-Agent.
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=socialaccount_app_settings.REQUESTS_TIMEOUT
            ),
            **settings.AIOHTTP_CONFIG,
        )


class CustomDefaultAccountAdapter(AsyncDefaultAccountAdapter):
    def send_mail(self, template_prefix, email, context):
        # Single chokepoint for all allauth mail (email confirmation, password
        # reset, MFA notices). When email is disabled, skip without rendering or
        # sending -- callers like the password-reset endpoint still return their
        # normal response, they just don't deliver mail.
        if not settings.EMAIL_ENABLED:
            return
        return super().send_mail(template_prefix, email, context)

    async def asend_mail(self, template_prefix, email, context):
        if not settings.EMAIL_ENABLED:
            return
        return await super().asend_mail(template_prefix, email, context)

    def render_mail(self, template_prefix, email, context, headers=None):
        headers = headers or {}
        default_headers = GlitchTipEmail.get_default_headers()
        for key, value in default_headers.items():
            if key not in headers:
                headers[key] = value
        return super().render_mail(template_prefix, email, context, headers)

    def is_open_for_signup(self, request):
        return is_user_registration_open()

    async def ais_open_for_signup(self, request):
        return await ais_user_registration_open()

    def save_user(self, request, user, form, commit=True):
        # Consider a signup a form of authentication
        user = super().save_user(request, user, form, commit)
        if commit:
            record_authentication(request, user, method="signup")
        return user

    async def asave_user(self, request, user, form, commit=True):
        user = await super().asave_user(request, user, form, commit)
        if commit:
            await arecord_authentication(request, user, method="signup")
        return user
