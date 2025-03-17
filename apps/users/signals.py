import logging

from allauth.account.signals import user_logged_in
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.db.models import Prefetch
from django.dispatch import receiver

from apps.organizations_ext.models import (
    OrganizationUser,
)

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def add_user_to_socialapp_organization(request, user, **kwargs):
    """
    Add user to organization if organization-social app exists
    """
    social_apps = (
        SocialApp.objects.filter(
            provider__in=SocialAccount.objects.filter(user=user).values_list(
                "provider", flat=True
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
    for social_app in social_apps:
        if not social_app.organizationsocialapp.organization.matched_user:  # type: ignore
            social_app.organizationsocialapp.organization.add_user(user)  # type: ignore
            logger.info(
                f"Added {social_app.organizationsocialapp.organization} to {user}"
            )  # type: ignore
