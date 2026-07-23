from django.conf import settings
from django.db.utils import IntegrityError

from apps.organizations_ext.models import Organization


async def is_organization_creation_open() -> bool:
    return (
        settings.ENABLE_ORGANIZATION_CREATION
        or not await Organization.objects.aexists()
    )


async def organization_can_customize_sso(organization: Organization) -> bool:
    """
    Whether an organization may manage its own SSO (social apps) via the API.

    Self-hosted installs (billing disabled) always may; the server admin can
    further restrict access with Django admin permissions. On billing-enabled
    installs the organization needs an active subscription to a plan whose
    Stripe product carries the sso metadata flag.
    """
    if not settings.BILLING_ENABLED:
        return True
    # Imported here because apps.stripe imports this app at module load.
    from apps.stripe.constants import ACTIVE_SUBSCRIPTION_STATUSES
    from apps.stripe.models import StripeSubscription

    return await StripeSubscription.objects.filter(
        organization=organization,
        status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        price__product__allow_sso=True,
    ).aexists()


def is_member_guard_violation(exc: IntegrityError) -> bool:
    """
    True when the database rejected a delete via the organization_has_members
    constraint trigger, i.e. the transaction would have left a live
    organization with no members.
    """
    diag = getattr(exc.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == "organization_has_members"
