import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from allauth.socialaccount.models import SocialApp
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from organizations.abstract import SharedBaseModel
from organizations.base import (
    OrganizationBase,
    OrganizationInvitationBase,
    OrganizationOwnerBase,
    OrganizationUserBase,
)
from organizations.managers import OrgManager
from organizations.signals import owner_changed

from apps.difs.models import DebugInformationFile
from apps.observability.utils import clear_metrics_cache
from apps.projects.models import (
    IssueEventProjectHourlyStatistic,
    LogProjectHourlyStatistic,
    TransactionEventProjectHourlyStatistic,
)
from apps.sourcecode.models import DebugSymbolBundle
from apps.stripe.utils import compute_cycle_n_ago
from apps.uptime.models import UptimeCheckHourlyStatistic

from .constants import OrganizationUserRole
from .fields import OrganizationSlugField

logger = logging.getLogger(__name__)


class OrganizationManager(OrgManager):
    pass


# Billed weight per raw category (uptime/logs are weighted 0.1: 10 raw = 1 billed).
# Single source of truth for usage weighting; don't re-weight elsewhere.
USAGE_WEIGHTS = {
    "issue_event_count": 1,
    "transaction_count": 1,
    "log_count": 0.1,
    "uptime_check_event_count": 0.1,
    "file_size": 1,
}


@dataclass
class EventCounts:
    """Raw per-category usage counts plus billed values (weights: USAGE_WEIGHTS)."""

    issue_event_count: int = 0
    transaction_count: int = 0
    log_count: int = 0
    uptime_check_event_count: int = 0
    file_size: int = 0

    def billed(self, field: str) -> float:
        """Unfloored billed contribution of one raw category, e.g. 50 uptime -> 5.0."""
        return getattr(self, field) * USAGE_WEIGHTS[field]

    @property
    def total_event_count(self) -> int:
        """Weighted billed total, floored exactly once."""
        return int(sum(getattr(self, f) * w for f, w in USAGE_WEIGHTS.items()))


async def get_event_counts(
    org_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> EventCounts:
    """
    Get event counts for an organization using separate queries per table.

    Unlike with_event_counts() which builds one SQL statement with correlated
    subqueries across all partitioned tables (acquiring locks on every partition
    simultaneously), this runs separate aggregate queries. Each query only locks
    one table's partitions and releases them before the next query runs.
    """
    date_filter = Q()
    created_filter = Q()
    if start and end:
        date_filter = Q(date__gte=start, date__lt=end)
        created_filter = Q(created__gte=start, created__lt=end)

    issue_result = await IssueEventProjectHourlyStatistic.objects.filter(
        Q(organization_id=org_id) & date_filter
    ).aaggregate(total=Coalesce(Sum("count"), 0))

    transaction_result = await TransactionEventProjectHourlyStatistic.objects.filter(
        Q(organization_id=org_id) & date_filter
    ).aaggregate(total=Coalesce(Sum("count"), 0))

    log_result = await LogProjectHourlyStatistic.objects.filter(
        Q(organization_id=org_id) & date_filter
    ).aaggregate(total=Coalesce(Sum("count"), 0))

    uptime_result = await UptimeCheckHourlyStatistic.objects.filter(
        Q(organization_id=org_id) & date_filter
    ).aaggregate(total=Coalesce(Sum("count"), 0))

    symbol_result = await DebugSymbolBundle.objects.filter(
        Q(organization_id=org_id) & created_filter
    ).aaggregate(total=Coalesce(Sum("file__blob__size"), 0))

    info_result = await DebugInformationFile.objects.filter(
        Q(project__organization_id=org_id) & created_filter
    ).aaggregate(total=Coalesce(Sum("file__blob__size"), 0))

    file_size = int((symbol_result["total"] + info_result["total"]) / 1000000)

    return EventCounts(
        issue_event_count=issue_result["total"],
        transaction_count=transaction_result["total"],
        log_count=log_result["total"],
        uptime_check_event_count=uptime_result["total"],
        file_size=file_size,
    )


SELF_HOSTED_USAGE_WINDOW_DAYS = 30


def rolling_period(
    periods_ago: int = 0, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Rolling 30-day usage window for self-hosted installs (no billing cycle).

    Self-hosters have no Stripe cycle, but they still pay for usage in hardware,
    so we report it over a rolling month. periods_ago=0 -> (now - 30d, now);
    periods_ago=1 -> (now - 60d, now - 30d).
    """
    if now is None:
        now = timezone.now()
    window = timedelta(days=SELF_HOSTED_USAGE_WINDOW_DAYS)
    end = now - window * periods_ago
    return end - window, end


def get_free_tier_cycle(
    created: datetime, periods_ago: int = 0
) -> tuple[datetime, datetime]:
    """Monthly usage cycle anchored to an organization's creation date.

    Used for SaaS organizations without an active Stripe subscription (the free
    tier, or a lapsed subscription). Stripe is the source of truth whenever a
    subscription exists; without one we still need a stable monthly window, so
    we anchor it to the signup date exactly as throttling does. periods_ago=1 is
    the immediately preceding cycle.
    """
    now = timezone.now()
    if created > now:
        current_index = 0
    else:
        # How many whole months since signup contain `now`.
        current_index = (now.year - created.year) * 12 + now.month - created.month
        if created + relativedelta(months=current_index) > now:
            current_index -= 1
    # Re-anchor every boundary on `created` so adjacent cycles stay contiguous
    # even when the signup day (29-31) gets clamped in shorter months.
    index = current_index - periods_ago
    return (
        created + relativedelta(months=index),
        created + relativedelta(months=index + 1),
    )


async def get_current_period_dates(
    org: "Organization", periods_ago: int = 0
) -> tuple[datetime, datetime] | None:
    """Resolve the usage-reporting window for an organization, N periods back.

    Single source of truth shared by the usage API, the admin, and throttling so
    the number a user sees matches the window we actually enforce:

    - Self-hosted (BILLING_ENABLED=False): a rolling 30-day window.
    - SaaS with an active subscription: the Stripe cycle dates — Stripe is the
      source of truth (handles annual plans with virtual monthly cycles).
    - SaaS without an active subscription: a monthly cycle anchored to the org's
      creation date, mirroring how the free tier is throttled.

    Returns None only when periods_ago points to a cycle before an annual
    subscription began (matching compute_cycle_n_ago).
    """
    if not settings.BILLING_ENABLED:
        return rolling_period(periods_ago)

    sub = await (
        type(org)
        .objects.filter(pk=org.pk)
        .values_list(
            "stripe_primary_subscription__subscription_cycle_start",
            "stripe_primary_subscription__subscription_cycle_end",
            "stripe_primary_subscription__current_period_start",
            "stripe_primary_subscription__current_period_end",
        )
        .afirst()
    )
    cycle_start, cycle_end, period_start, period_end = (
        sub if sub else (None, None, None, None)
    )

    if period_start and period_end:
        # Active subscription: Stripe is the source of truth.
        if periods_ago == 0:
            return cycle_start or period_start, cycle_end or period_end
        return compute_cycle_n_ago(
            period_start, period_end, cycle_start, cycle_end, periods_ago
        )

    # No active subscription: mimic the free tier with our own anchor.
    return get_free_tier_cycle(org.created, periods_ago)


class Organization(SharedBaseModel, OrganizationBase):
    id = models.AutoField(primary_key=True)  # BIGINT is unnecessary
    slug = OrganizationSlugField(
        max_length=200,
        blank=False,
        editable=True,
        populate_from="name",
        unique=True,
        help_text=_("The name in all lowercase, suitable for URL identification"),
    )
    is_accepting_events = models.BooleanField(
        default=True, help_text="Used for throttling at org level"
    )
    event_throttle_rate = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
        help_text="Probability (in percent) on how many events are throttled. Used for throttling at project level",
    )
    open_membership = models.BooleanField(
        default=True, help_text="Allow any organization member to join any team"
    )
    scrub_ip_addresses = models.BooleanField(
        default=True,
        help_text="Default for whether projects should script IP Addresses",
    )
    stripe_customer_id = models.CharField(max_length=28, blank=True)
    stripe_primary_subscription = models.ForeignKey(
        "stripe.StripeSubscription",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="+",
    )
    is_deleted = models.BooleanField(default=False)

    objects = OrganizationManager()

    def save(self, *args, **kwargs):
        new = False
        if not self.pk:
            new = True
        super().save(*args, **kwargs)
        if new:
            clear_metrics_cache()

    def delete(self, *args, **kwargs):
        """Soft-delete: mark as deleted and enqueue async cleanup."""
        from apps.organizations_ext.tasks import delete_organization

        self.is_deleted = True
        self.save(update_fields=["is_deleted"])
        delete_organization.enqueue(self.pk)

    def force_delete(self, *args, **kwargs):
        """Actually delete the organization and all related data from the DB."""
        super().delete(*args, **kwargs)
        clear_metrics_cache()

    def slugify_function(self, content):
        reserved_words = [
            "login",
            "register",
            "app",
            "profile",
            "organizations",
            "settings",
            "issues",
            "performance",
            "_health",
            "rest-auth",
            "api",
            "accept",
            "stripe",
            "admin",
            "status_page",
            "__debug__",
        ]
        slug = slugify(content)
        if slug in reserved_words:
            return slug + "-1"
        return slug

    def add_user(self, user, role=OrganizationUserRole.MEMBER):
        """
        Adds a new user and if the first user makes the user an owner.
        """
        users_count = self.users.all().count()
        if users_count == 0:
            role = OrganizationUserRole.OWNER
        # super() sends user_added before the owner record below exists;
        # receivers must not rely on self.owner for the first user.
        org_user = super().add_user(user, role=role)
        if users_count == 0:
            self._org_owner_model.objects.create(
                organization=self, organization_user=org_user
            )
        return org_user

    async def aadd_user(self, user, role=OrganizationUserRole.MEMBER):
        """Async version of ``add_user``."""
        users_count = await self.users.acount()
        if users_count == 0:
            role = OrganizationUserRole.OWNER
        org_user = await super().aadd_user(user, role=role)
        if users_count == 0:
            await self._org_owner_model.objects.acreate(
                organization=self, organization_user=org_user
            )
        return org_user

    @property
    def owners(self):
        return self.users.filter(
            organizations_ext_organizationuser__role=OrganizationUserRole.OWNER
        )

    @property
    def email(self) -> str | None:
        """
        Used to identify billing contact for stripe.

        Returns the email of the designated OrganizationOwner (billing contact).
        Falls back to the first user with OWNER role if no OrganizationOwner exists.
        """
        try:
            billing_contact = self.owner.organization_user.user
            return billing_contact.email
        except self._org_owner_model.DoesNotExist:
            logger.warning(
                "Organization %s (id=%s) has no OrganizationOwner. "
                "This indicates a data integrity issue.",
                self.slug,
                self.id,
            )
            # Fallback to first user with OWNER role
            first_owner = self.owners.first()
            if first_owner:
                return first_owner.email
            return None

    def get_user_scopes(self, user):
        org_user = self.organization_users.get(user=user)
        return org_user.get_scopes()

    def change_owner(self, new_owner):
        """
        Changes ownership of an organization.
        """
        old_owner = self.owner.organization_user
        self.owner.organization_user = new_owner
        self.owner.save()

        owner_changed.send(sender=self, old=old_owner, new=new_owner)

    async def achange_owner(self, new_owner):
        """Async version of ``change_owner``."""
        # Fetch the owner explicitly; the lazy ``self.owner`` accessor would
        # raise SynchronousOnlyOperation when not already loaded.
        owner = await self._org_owner_model.objects.select_related(
            "organization_user"
        ).aget(organization=self)
        old_owner = owner.organization_user
        owner.organization_user = new_owner
        await owner.asave()

        await owner_changed.asend(sender=self, old=old_owner, new=new_owner)

    def is_owner(self, user):
        """
        Returns True is user is the organization's owner, otherwise false
        """
        return self.owner.organization_user.user == user


class OrganizationUser(SharedBaseModel, OrganizationUserBase):
    user = models.ForeignKey(
        "users.User",
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="organizations_ext_organizationuser",
    )
    role = models.PositiveSmallIntegerField(choices=OrganizationUserRole.choices)
    email = models.EmailField(
        blank=True, null=True, help_text="Email for pending invite"
    )

    class Meta(OrganizationOwnerBase.Meta):
        unique_together = (("user", "organization"), ("email", "organization"))

    def __str__(self, *args, **kwargs):
        if self.user:
            return super().__str__(*args, **kwargs)
        return self.email

    def get_email(self):
        if self.user:
            return self.user.email
        return self.email

    def get_role(self):
        return self.get_role_display().lower()

    def get_scopes(self):
        role = OrganizationUserRole.get_role(self.role)
        return role["scopes"]

    @property
    def pending(self):
        return self.user_id is None

    @property
    def is_active(self):
        """Non pending means active"""
        return not self.pending


class OrganizationOwner(OrganizationOwnerBase):
    """Only usage is for billing contact currently"""


class OrganizationInvitation(OrganizationInvitationBase):
    """Required to exist for django-organizations"""


class OrganizationSocialApp(models.Model):
    """
    Associate organization with social app, for authentication purposes.
    Example: If Foo org has social app FooGoogle, then any user logging in via FooGoogle
    OAuth must be automatically assigned to the Foo org.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    social_app = models.OneToOneField(SocialApp, on_delete=models.CASCADE)
