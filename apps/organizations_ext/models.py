from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import Exists, F, OuterRef, Q
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
from sql_util.utils import SubqueryCount, SubquerySum

from apps.observability.metrics import clear_metrics_cache

from .constants import OrganizationUserRole
from .fields import OrganizationSlugField


class OrganizationManager(OrgManager):
    def with_event_counts(self, current_period=True):
        queryset = self
        subscription_filter = Q()
        event_subscription_filter = Q()
        checks_subscription_filter = Q()
        if current_period and settings.BILLING_ENABLED:
            from djstripe.models import Subscription

            subscription_filter = Q(
                created__gte=OuterRef(
                    "djstripe_customers__subscriptions__current_period_start"
                ),
                created__lt=OuterRef(
                    "djstripe_customers__subscriptions__current_period_end"
                ),
            )
            event_subscription_filter = Q(
                date__gte=OuterRef(
                    "djstripe_customers__subscriptions__current_period_start"
                ),
                date__lt=OuterRef(
                    "djstripe_customers__subscriptions__current_period_end"
                ),
            )
            checks_subscription_filter = Q(
                start_check__gte=OuterRef(
                    "djstripe_customers__subscriptions__current_period_start"
                ),
                start_check__lt=OuterRef(
                    "djstripe_customers__subscriptions__current_period_end"
                ),
            )
            # If the org has an active sub, filter by it. If not, do not filter.
            # This forces the exclusion of inactive subscriptions in subquery counts
            queryset = queryset.annotate(
                has_active_subscription=Exists(
                    Subscription.objects.filter(
                        customer=OuterRef("djstripe_customers"),
                        status="active",
                    )
                )
            ).filter(
                Q(
                    has_active_subscription=True,
                    djstripe_customers__subscriptions__status="active",
                )
                | Q(has_active_subscription=False)
            )

        queryset = queryset.annotate(
            issue_event_count=Coalesce(
                SubquerySum(
                    "projects__issueeventprojecthourlystatistic__count",
                    filter=event_subscription_filter,
                ),
                0,
            ),
            transaction_count=Coalesce(
                SubquerySum(
                    "projects__transactioneventprojecthourlystatistic__count",
                    filter=event_subscription_filter,
                ),
                0,
            ),
            uptime_check_event_count=SubqueryCount(
                "monitor__checks", filter=checks_subscription_filter
            ),
            file_size=(
                Coalesce(
                    SubquerySum(
                        "debugsymbolbundle__file__blob__size",
                        filter=subscription_filter,
                    ),
                    0,
                )
                + Coalesce(
                    SubquerySum(
                        "projects__debuginformationfile__file__blob__size",
                        filter=subscription_filter,
                    ),
                    0,
                )
            )
            / 1000000,
            total_event_count=F("issue_event_count")
            + F("transaction_count")
            + F("uptime_check_event_count")
            + F("file_size"),
        )
        return queryset.distinct("pk")


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
