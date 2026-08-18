from django.conf import settings
from django.db.utils import IntegrityError

from apps.organizations_ext.models import Organization
from apps.uptime.constants import DEFAULT_MAX_MONITORS_PER_ORGANIZATION


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


async def get_uptime_monitor_limit(organization: Organization) -> int | None:
    """
    How many uptime monitors this organization may have, or None for no limit.

    Every monitor is a standing instruction to send traffic on a schedule, so an
    uncapped account can aim an arbitrary number of them at one third party and
    use the check dispatcher as an amplifier. Unlike event volume, that costs
    the abuser nothing per check and the billing throttle only reacts on its
    next sweep, so the ceiling has to be enforced at creation time.

    Self-hosted installs (billing disabled) are unlimited: the operator decides
    who gets an account and can remove one. Billing-enabled installs accept
    anonymous signups, so they always have a limit -- taken from the subscribed
    plan's Stripe ``uptime_monitors`` metadata, falling back to
    ``DEFAULT_MAX_MONITORS_PER_ORGANIZATION``. Where several active
    subscriptions apply, the most generous wins.
    """
    if not settings.BILLING_ENABLED:
        return None
    # Imported here because apps.stripe imports this app at module load.
    from apps.stripe.constants import ACTIVE_SUBSCRIPTION_STATUSES
    from apps.stripe.models import StripeSubscription

    plan_limit = (
        await StripeSubscription.objects.filter(
            organization=organization,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            price__product__uptime_monitors__isnull=False,
        )
        .order_by("-price__product__uptime_monitors")
        .values_list("price__product__uptime_monitors", flat=True)
        .afirst()
    )
    if plan_limit is not None:
        return plan_limit
    return DEFAULT_MAX_MONITORS_PER_ORGANIZATION


def is_member_guard_violation(exc: IntegrityError) -> bool:
    """
    True when the database rejected a delete via the organization_has_members
    constraint trigger, i.e. the transaction would have left a live
    organization with no members.
    """
    diag = getattr(exc.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == "organization_has_members"
