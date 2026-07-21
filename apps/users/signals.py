import logging

from allauth.account.signals import user_logged_in
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.db.models import Prefetch, Q
from django.dispatch import receiver

from apps.organizations_ext.models import (
    OrganizationUser,
)

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
async def add_user_to_socialapp_organization(request, user, **kwargs):
    """
    Add user to organization if organization-social app exists
    """
    # Lazy queryset: composes into the SocialApp filter as a SQL subquery,
    # so no standalone query runs here (async-safe either way).
    user_providers = SocialAccount.objects.filter(user=user).values_list(
        "provider", flat=True
    )
    social_apps = (
        SocialApp.objects.filter(
            # Check for provider_id or provider if the former is not set
            (
                ~(Q(provider_id__isnull=True) | Q(provider_id__exact=""))
                & Q(provider_id__in=user_providers)
            )
            | (
                (Q(provider_id__isnull=True) | Q(provider_id__exact=""))
                & Q(provider__in=user_providers)
            )
        )
        .exclude(organizationsocialapp=None)
        .select_related("organizationsocialapp__organization")
        .prefetch_related(
            Prefetch(
                "organizationsocialapp__organization__organization_users",
                queryset=OrganizationUser.objects.filter(user=user),
                to_attr="matched_user",
            )
        )
        .all()
    )
    async for social_app in social_apps:
        if not social_app.organizationsocialapp.organization.matched_user:  # type: ignore
            await social_app.organizationsocialapp.organization.aadd_user(user)  # type: ignore
            logger.info(
                f"Added {social_app.organizationsocialapp.organization} to {user}"
            )  # type: ignore
