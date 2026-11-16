import logging

from allauth.account.signals import user_logged_in
from allauth.socialaccount.models import SocialAccount
from django.db.models import Prefetch
from django.dispatch import receiver

from apps.organizations_ext.models import (
    OrganizationUser,
)
from apps.users.utils import org_social_apps_for_provider

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
        org_social_apps_for_provider(user_providers)
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
        organization = social_app.organizationsocialapp.organization  # type: ignore
        if not organization.matched_user:
            # Note the first member of a memberless organization becomes its
            # OWNER (aadd_user) -- deliberate, so a fresh org set up with only
            # a social app bootstraps on first SSO login.
            await organization.aadd_user(user)
            logger.info(f"Added {organization} to {user}")
