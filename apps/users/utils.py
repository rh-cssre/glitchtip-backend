from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.db.models import Q, QuerySet

from apps.users.models import User


def org_social_apps_for_provider(provider: str) -> QuerySet[SocialApp]:
    """
    Organization-linked social apps matching a SocialAccount provider string,
    which holds the app's provider_id when set (multi-instance providers like
    OIDC) and otherwise the provider name.
    """
    empty_provider_id = Q(provider_id__isnull=True) | Q(provider_id__exact="")
    return SocialApp.objects.filter(
        (~empty_provider_id & Q(provider_id=provider))
        | (empty_provider_id & Q(provider=provider))
    ).exclude(organizationsocialapp=None)


def org_social_app_exists(provider: str) -> bool:
    return org_social_apps_for_provider(provider).exists()


async def aorg_social_app_exists(provider: str) -> bool:
    return await org_social_apps_for_provider(provider).aexists()


def is_user_registration_open() -> bool:
    return settings.ENABLE_USER_REGISTRATION or not User.objects.exists()


async def ais_user_registration_open() -> bool:
    return settings.ENABLE_USER_REGISTRATION or not await User.objects.aexists()


def is_social_apps_user_registration_open() -> bool:
    return settings.ENABLE_SOCIAL_APPS_USER_REGISTRATION or not User.objects.exists()


async def ais_social_apps_user_registration_open() -> bool:
    return (
        settings.ENABLE_SOCIAL_APPS_USER_REGISTRATION
        or not await User.objects.aexists()
    )
