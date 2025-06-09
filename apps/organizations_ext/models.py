from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import Count, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
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
from organizations.signals import owner_changed, user_added

from apps.difs.models import DebugInformationFile
from apps.observability.utils import clear_metrics_cache
from apps.projects.models import (
    IssueEventProjectHourlyStatistic,
    TransactionEventProjectHourlyStatistic,
)
from apps.sourcecode.models import DebugSymbolBundle
from apps.uptime.models import MonitorCheck

from .constants import OrganizationUserRole
from .fields import OrganizationSlugField


class OrganizationManager(OrgManager):
    def with_event_counts(self, current_period=True):
        queryset = self
        subscription_filter = Q()
        event_subscription_filter = Q()
        checks_subscription_filter = Q()
        if current_period and settings.BILLING_ENABLED:
            subscription_filter = Q(
                created__gte=OuterRef(
                    "stripe_primary_subscription__current_period_start"
                ),
                created__lt=OuterRef("stripe_primary_subscription__current_period_end"),
            )
            event_subscription_filter = Q(
                date__gte=OuterRef("stripe_primary_subscription__current_period_start"),
                date__lt=OuterRef("stripe_primary_subscription__current_period_end"),
            )
            checks_subscription_filter = Q(
                start_check__gte=OuterRef(
                    "stripe_primary_subscription__current_period_start"
                ),
                start_check__lt=OuterRef(
                    "stripe_primary_subscription__current_period_end"
                ),
            )

        # Subquery for Issue Events Sum
        issue_event_subquery = Subquery(
            IssueEventProjectHourlyStatistic.objects.filter(
                Q(project__organization=OuterRef("pk")),  # Link to outer Organization
                event_subscription_filter,  # Apply date filtering
            )
            .values(
                "project__organization"  # Group by organization (required for annotate)
            )
            .annotate(
                sum_count=Sum("count")  # Calculate sum for this group
            )
            .values(
                "sum_count"  # Select only the calculated sum
            )
            .order_by(),  # Prevent potential default ordering issues in subquery
            output_field=models.BigIntegerField(),  # Define output type
        )

        # Subquery for Transaction Events Sum
        transaction_subquery = Subquery(
            TransactionEventProjectHourlyStatistic.objects.filter(
                Q(project__organization=OuterRef("pk")), event_subscription_filter
            )
            .values("project__organization")
            .annotate(sum_count=Sum("count"))
            .values("sum_count")
            .order_by(),
            output_field=models.BigIntegerField(),
        )

        # Subquery for Uptime Checks Count
        # Assumes MonitorCheck relates to Monitor which relates to Organization
        uptime_check_subquery = Subquery(
            MonitorCheck.objects.filter(
                Q(monitor__organization=OuterRef("pk")),  # Link Monitor -> Organization
                Q(checks_subscription_filter),  # Apply date filtering
            )
            .values(
                "monitor__organization"  # Group by organization
            )
            .annotate(
                check_count=Count("pk")  # Count checks for this group
            )
            .values(
                "check_count"  # Select only the count
            )
            .order_by(),
            output_field=models.IntegerField(),
        )

        # Subquery for Debug Symbol Bundle File Size Sum
        # Assumes DebugSymbolBundle relates directly to Organization
        debugsymbol_size_subquery = Subquery(
            DebugSymbolBundle.objects.filter(
                Q(organization=OuterRef("pk")),  # Direct link to Organization
                Q(subscription_filter),  # Apply created date filtering
            )
            .values(
                "organization"  # Group by organization
            )
            .annotate(
                total_size=Sum("file__blob__size")  # Sum blob sizes
            )
            .values(
                "total_size"  # Select the sum
            )
            .order_by(),
            output_field=models.BigIntegerField(),
        )

        # Subquery for Debug Information File Size Sum
        # Assumes DebugInformationFile relates to Project which relates to Organization
        debuginfo_size_subquery = Subquery(
            DebugInformationFile.objects.filter(
                Q(project__organization=OuterRef("pk")),  # Link via Project
                subscription_filter,  # Apply created date filtering
            )
            .values(
                "project__organization"  # Group by organization
            )
            .annotate(
                total_size=Sum("file__blob__size")  # Sum blob sizes
            )
            .values(
                "total_size"  # Select the sum
            )
            .order_by(),
            output_field=models.BigIntegerField(),
        )
        return queryset.annotate(
            issue_event_count=Coalesce(issue_event_subquery, 0),
            transaction_count=Coalesce(transaction_subquery, 0),
            # Use Coalesce for count as well, safer if no checks exist
            uptime_check_event_count=Coalesce(uptime_check_subquery, 0),
            # Calculate total file size, Coalesce each part, sum, then convert/divide
            # Use FloatField for output if division result can be non-integer
            file_size=Coalesce(
                models.ExpressionWrapper(
                    (
                        Coalesce(debugsymbol_size_subquery, 0)
                        + Coalesce(debuginfo_size_subquery, 0)
                    ),
                    output_field=models.FloatField(),  # Cast sum before division
                )
                / 1000000.0,  # Divide by 1 million (ensure float division)
                0.0,  # Coalesce the final division result
                output_field=models.BigIntegerField(),
            ),
        ).annotate(
            # Calculate total using F expressions referring to the fields just annotated
            total_event_count=F("issue_event_count")
            + F("transaction_count")
            + F("uptime_check_event_count")
            # Note: Adding file_size (in MB) directly to event counts might be conceptually odd.
            # Verify if this addition is intended business logic.
            # If file_size should not be part of 'total_event_count', remove it here.
            + F("file_size"),
        )


class Organization(SharedBaseModel, OrganizationBase):
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

    objects = OrganizationManager()

    def save(self, *args, **kwargs):
        new = False
        if not self.pk:
            new = True
        super().save(*args, **kwargs)
        if new:
            clear_metrics_cache()

    def delete(self, *args, **kwargs):
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
        Adds a new user and if the first user makes the user an admin and
        the owner.
        """
        users_count = self.users.all().count()
        if users_count == 0:
            role = OrganizationUserRole.OWNER
        org_user = self._org_user_model.objects.create(
            user=user, organization=self, role=role
        )
        if users_count == 0:
            self._org_owner_model.objects.create(
                organization=self, organization_user=org_user
            )

        # User added signal
        user_added.send(sender=self, user=user)
        return org_user

    @property
    def owners(self):
        return self.users.filter(
            organizations_ext_organizationuser__role=OrganizationUserRole.OWNER
        )

    @property
    def email(self):
        """Used to identify billing contact for stripe."""
        billing_contact = self.owner.organization_user.user
        return billing_contact.email

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
