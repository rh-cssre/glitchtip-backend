import functools
import ipaddress
import socket

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


@functools.cache
def _public_only_classes():
    """Define PublicOnlyResolver and PublicOnlyConnector on first use.

    They subclass aiohttp types, and allauth loads this module on the first
    auth request whether or not social login is configured. Defining them here
    keeps aiohttp out of the process until a social-auth session is opened (see
    glitchtip.ssrf_protection). Module ``__getattr__`` below still exposes them
    under their usual names.
    """
    import aiohttp

    class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
        """
        DNS resolver that refuses to yield private/internal addresses.

        Organization-managed OIDC providers make social-auth URLs partially
        user-controlled — not just the issuer an org manager types, but every
        endpoint listed in the discovery document it serves. Filtering at
        resolution time (rather than validating URL strings) is what also
        defeats DNS tricks such as rebinding a public hostname to
        169.254.169.254.
        """

        def __init__(self):
            self._resolver = aiohttp.DefaultResolver()

        async def resolve(self, host, port=0, family=socket.AF_INET):
            infos = await self._resolver.resolve(host, port, family)
            for info in infos:
                if not ipaddress.ip_address(info["host"]).is_global:
                    raise aiohttp.ClientConnectionError(
                        f"Refusing to connect to non-public address for {host}"
                    )
            return infos

        async def close(self):
            await self._resolver.close()

    class PublicOnlyConnector(aiohttp.TCPConnector):
        """
        TCPConnector that refuses private/internal destinations.

        The resolver alone is not enough: TCPConnector._resolve_host
        short-circuits IP-literal hosts without ever consulting the resolver,
        so a discovery document (or redirect) pointing at
        https://169.254.169.254/ would connect. Literal IPs are checked here;
        hostnames still resolve through PublicOnlyResolver, which validates
        every resolved address.
        """

        def __init__(self, **kwargs):
            super().__init__(resolver=PublicOnlyResolver(), **kwargs)
            # TCPConnector treats a passed-in resolver as externally owned and
            # never closes it; this one is ours, so close it with the connector.
            self._resolver_owner = True

        async def _resolve_host(self, host, port, traces=None):
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                pass  # Hostname: PublicOnlyResolver vets the resolved addresses
            else:
                if not ip.is_global:
                    raise aiohttp.ClientConnectionError(
                        f"Refusing to connect to non-public address {host}"
                    )
            return await super()._resolve_host(host, port, traces)

    return {
        "PublicOnlyResolver": PublicOnlyResolver,
        "PublicOnlyConnector": PublicOnlyConnector,
    }


def __getattr__(name):
    if name in ("PublicOnlyResolver", "PublicOnlyConnector"):
        return _public_only_classes()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        # path used.
        import aiohttp

        from glitchtip.ssrf_protection import session_kwargs

        kwargs = session_kwargs(
            timeout=aiohttp.ClientTimeout(
                total=socialaccount_app_settings.REQUESTS_TIMEOUT
            ),
        )
        if settings.SOCIAL_AUTH_BLOCK_PRIVATE_IPS:
            kwargs["connector"] = _public_only_classes()["PublicOnlyConnector"]()
        return aiohttp.ClientSession(**kwargs)


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
