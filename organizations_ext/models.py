from django.db import models
from django.utils.translation import ugettext_lazy as _
from organizations.base import (
    OrganizationBase,
    OrganizationUserBase,
    OrganizationOwnerBase,
)
from organizations.abstract import SharedBaseModel
from organizations.fields import SlugField
from organizations.signals import user_added


class OrganizationUserRole(models.IntegerChoices):
    MEMBER = 0, "Member"
    ADMIN = 1, "Admin"
    MANAGER = 2, "Manager"
    OWNER = 3, "Owner"  # Many users can be owner but only one primary owner


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
        """ Used to identify billing contact for stripe. """
        billing_contact = self.owner.organization_user.user
        return billing_contact.email


class OrganizationUser(SharedBaseModel, OrganizationUserBase):
    role = models.PositiveSmallIntegerField(choices=OrganizationUserRole.choices)


class OrganizationOwner(OrganizationOwnerBase):
    """ Only usage is for billing contact currently """
