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
    OWNER = 3, "Owner"


class Organization(SharedBaseModel, OrganizationBase):
    slug = SlugField(
        max_length=200,
        blank=False,
        editable=True,
        populate_from="name",
        unique=True,
        help_text=_("The name in all lowercase, suitable for URL identification"),
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


class OrganizationUser(SharedBaseModel, OrganizationUserBase):
    role = models.PositiveSmallIntegerField(choices=OrganizationUserRole.choices)


class OrganizationOwner(OrganizationOwnerBase):
    pass
