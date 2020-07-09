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

    @classmethod
    def from_string(cls, string: str):
        for status in cls:
            if status.label.lower() == string.lower():
                return status


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

    def accept_invite(self, user):
        self.user = user
        self.email = None
        self.save()

    @property
    def pending(self):
        return self.user_id is None

    @property
    def is_active(self):
        """ Non pending means active """
        return not self.pending


class OrganizationOwner(OrganizationOwnerBase):
    """ Only usage is for billing contact currently """
