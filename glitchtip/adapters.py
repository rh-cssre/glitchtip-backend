from allauth.account.internal.flows.login import record_authentication
from allauth.socialaccount import app_settings as socialaccount_app_settings
from allauth_async.account.adapter import AsyncDefaultAccountAdapter
from allauth_async.account.internal.flows.login import arecord_authentication
from allauth_async.socialaccount.adapter import AsyncDefaultSocialAccountAdapter
from django.conf import settings

from apps.users.utils import (
    ais_social_apps_user_registration_open,
    ais_user_registration_open,
    aorg_social_app_exists,
    is_social_apps_user_registration_open,
    is_user_registration_open,
    org_social_app_exists,
)
from glitchtip.email import GlitchTipEmail


class CustomSocialAccountAdapter(AsyncDefaultSocialAccountAdapter):
    # An organization-linked social app is an implicit invitation: proving
    # identity at the organization's IdP is what grants membership (see
    # apps.users.signals.add_user_to_socialapp_organization), so account
    # creation stays possible even when open social signup is disabled.
    def is_open_for_signup(self, request, sociallogin):
        return is_social_apps_user_registration_open() or org_social_app_exists(
            sociallogin.account.provider
        )

    async def ais_open_for_signup(self, request, sociallogin):
        return await ais_social_apps_user_registration_open() or (
            await aorg_social_app_exists(sociallogin.account.provider)
        )

    def open_http_session(self):
        # Match the outbound-HTTP behavior the sync stack got from `requests`
        # (which trusts proxy env vars by default) and stamp our User-Agent.
        # Social-auth calls override the generic 30s session timeout with
        # allauth's own REQUESTS_TIMEOUT, the same bound the sync `requests`
        # path used. Organization-managed OIDC providers make these URLs
        # partially user-controlled -- not just the issuer an org manager
        # types, but every endpoint its discovery document lists -- so they
        # get the same connect-time SSRF guard as webhooks and uptime checks.
        import aiohttp

        from glitchtip.ssrf_protection import safe_session

        return safe_session(
            allow_private=not settings.SOCIAL_AUTH_BLOCK_PRIVATE_IPS,
            timeout=aiohttp.ClientTimeout(
                total=socialaccount_app_settings.REQUESTS_TIMEOUT
            ),
        )

    def get_requests_session(self):
        # Only upstream's sync views still reach this: those allauth_async has
        # not twinned, today just headless token login (provider/token), which
        # fetches the IdP's discovery document and JWKS. `requests` has no
        # connect-time address check, so with blocking on, refuse rather than
        # let that path skip the guard open_http_session applies. allauth
        # turns the RequestException into an "invalid_token" response.
        if settings.SOCIAL_AUTH_BLOCK_PRIVATE_IPS:
            import requests

            raise requests.RequestException(
                "Sync social-auth HTTP is disabled while "
                "SOCIAL_AUTH_BLOCK_PRIVATE_IPS is on"
            )
        return super().get_requests_session()


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
