from django.conf import settings
from django.db import models
from django.db.models import F, OuterRef, Q
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
from organizations.fields import SlugField
from organizations.managers import OrgManager
from organizations.signals import user_added
from sql_util.utils import SubqueryCount, SubquerySum

# Defines which scopes belong to which role
# Credit to sentry/conf/server.py
ROLES = (
    {
        "id": "member",
        "name": "Member",
        "desc": "Members can view and act on events, as well as view most other data within the organization.",
        "scopes": set(
            [
                "event:read",
                "event:write",
                "event:admin",
                "project:releases",
                "project:read",
                "org:read",
                "member:read",
                "team:read",
            ]
        ),
    },
    {
        "id": "admin",
        "name": "Admin",
        "desc": "Admin privileges on any teams of which they're a member. They can create new teams and projects, as well as remove teams and projects which they already hold membership on (or all teams, if open membership is on). Additionally, they can manage memberships of teams that they are members of.",
        "scopes": set(
            [
                "event:read",
                "event:write",
                "event:admin",
                "org:read",
                "member:read",
                "project:read",
                "project:write",
                "project:admin",
                "project:releases",
                "team:read",
                "team:write",
                "team:admin",
                "org:integrations",
            ]
        ),
    },
    {
        "id": "manager",
        "name": "Manager",
        "desc": "Gains admin access on all teams as well as the ability to add and remove members.",
        "is_global": True,
        "scopes": set(
            [
                "event:read",
                "event:write",
                "event:admin",
                "member:read",
                "member:write",
                "member:admin",
                "project:read",
                "project:write",
                "project:admin",
                "project:releases",
                "team:read",
                "team:write",
                "team:admin",
                "org:read",
                "org:write",
                "org:integrations",
            ]
        ),
    },
    {
        "id": "owner",
        "name": "Organization Owner",
        "desc": "Unrestricted access to the organization, its data, and its settings. Can add, modify, and delete projects and members, as well as make billing and plan changes.",
        "is_global": True,
        "scopes": set(
            [
                "org:read",
                "org:write",
                "org:admin",
                "org:integrations",
                "member:read",
                "member:write",
                "member:admin",
                "team:read",
                "team:write",
                "team:admin",
                "project:read",
                "project:write",
                "project:admin",
                "project:releases",
                "event:read",
                "event:write",
                "event:admin",
            ]
        ),
    },
)


class OrganizationUserRole(models.IntegerChoices):
    MEMBER = 0, "Member"
    ADMIN = 1, "Admin"
    MANAGER = 2, "Manager"
    OWNER = 3, "Owner"  # Many users can be owner but only one primary owner

    @classmethod
    def from_string(cls, string: str):
        for status in cls:
            if status.label.lower() == string.lower():
                return status

    @classmethod
    def get_role(cls, role: int):
        return ROLES[role]


class OrganizationManager(OrgManager):
    def with_event_counts(self, current_period=True):
        subscription_filter = Q()
        event_subscription_filter = Q()
        if current_period and settings.BILLING_ENABLED:
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

        queryset = self.annotate(
            issue_event_count=Coalesce(
                SubquerySum(
                    "projects__eventprojecthourlystatistic__count",
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
                "monitor__checks", filter=subscription_filter
            ),
            file_size=(
                Coalesce(
                    SubquerySum(
                        "release__releasefile__file__blob__size",
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
    slug = SlugField(
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
    open_membership = models.BooleanField(
        default=True, help_text="Allow any organization member to join any team"
    )
    scrub_ip_addresses = models.BooleanField(
        default=True,
        help_text="Default for whether projects should script IP Addresses",
    )

    objects = OrganizationManager()

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

    def accept_invite(self, user):
        self.user = user
        self.email = None
        self.save()

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
