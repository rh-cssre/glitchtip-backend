"""An organization slug may never be all digits.

An all-digit path segment is reserved for an organization id, so allowing one
as a slug would let ``/organizations/42/`` name two organizations.
"""

import importlib
from contextlib import redirect_stdout
from io import StringIO

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.models import Organization

migration = importlib.import_module(
    "apps.organizations_ext.migrations.0017_organization_slug_not_all_digits"
)


class NumericSlugGuardTestCase(TestCase):
    async def test_all_digit_name_gets_a_suffix(self):
        organization = await baker.amake("organizations_ext.Organization", name="2024")
        self.assertEqual(organization.slug, "2024-1")

    async def test_a_second_all_digit_name_still_avoids_digits(self):
        await baker.amake("organizations_ext.Organization", name="1")
        organization = await baker.amake("organizations_ext.Organization", name="1")
        self.assertTrue(organization.slug.startswith("1-"))
        self.assertNotRegex(organization.slug, r"^[0-9]+$")

    async def test_names_that_merely_contain_digits_are_unchanged(self):
        for name, slug in [("12-34", "12-34"), ("007 agents", "007-agents")]:
            organization = await baker.amake(
                "organizations_ext.Organization", name=name
            )
            self.assertEqual(organization.slug, slug)

    async def test_api_create_never_yields_an_all_digit_slug(self):
        user = await baker.amake("users.user")
        await self.async_client.aforce_login(user)
        res = await self.async_client.post(
            reverse("api:list_organizations"),
            {"name": "1"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["slug"], "1-1")

    def test_validator_rejects_an_all_digit_slug(self):
        organization = baker.make("organizations_ext.Organization", name="acme")
        organization.slug = "123"
        with self.assertRaises(ValidationError) as ctx:
            organization.full_clean()
        self.assertIn("slug", ctx.exception.message_dict)

    def test_database_rejects_an_all_digit_slug(self):
        organization = baker.make("organizations_ext.Organization", name="acme")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Organization.objects.filter(id=organization.id).update(slug="123")


class RenameAllDigitSlugsMigrationTestCase(TestCase):
    """The migration's rename step, run against rows the constraint now forbids.

    The constraint is dropped inside the test's transaction so the old rows can
    be written, and comes back when the transaction rolls back.
    """

    def setUp(self):
        constraint = next(
            c
            for c in Organization._meta.constraints
            if c.name == "organization_slug_not_all_digits"
        )
        with connection.schema_editor() as editor:
            editor.remove_constraint(Organization, constraint)

    def make(self, slug, **kwargs):
        organization = baker.make("organizations_ext.Organization", **kwargs)
        Organization.objects.filter(id=organization.id).update(slug=slug)
        return organization

    def run_migration(self):
        out = StringIO()
        with redirect_stdout(out):
            migration.rename_all_digit_slugs(django_apps, None)
        return out.getvalue()

    def slug_of(self, organization):
        return Organization.objects.values_list("slug", flat=True).get(
            id=organization.id
        )

    def test_renames_to_the_first_free_suffix(self):
        plain = self.make("2024")
        taken = self.make("5")
        self.make("5-1")
        output = self.run_migration()

        self.assertEqual(self.slug_of(plain), "2024-1")
        self.assertEqual(self.slug_of(taken), "5-2")
        self.assertIn(f"organization {plain.id}: 2024 -> 2024-1", output)

    def test_renames_deleted_organizations_and_leading_zeros(self):
        deleted = self.make("42", is_deleted=True)
        zero = self.make("007")
        self.run_migration()
        self.assertEqual(self.slug_of(deleted), "42-1")
        self.assertEqual(self.slug_of(zero), "007-1")

    def test_a_maximum_length_slug_stays_within_the_column(self):
        organization = self.make("9" * 200)
        self.run_migration()
        slug = self.slug_of(organization)
        self.assertEqual(len(slug), 200)
        self.assertTrue(slug.endswith("-1"))

    def test_leaves_other_slugs_alone_and_is_silent_when_there_is_nothing(self):
        organization = self.make("acme-2024")
        self.assertEqual(self.run_migration(), "")
        self.assertEqual(self.slug_of(organization), "acme-2024")

    def test_the_constraint_can_be_added_afterwards(self):
        self.make("2024")
        self.run_migration()
        constraint = next(
            c
            for c in Organization._meta.constraints
            if c.name == "organization_slug_not_all_digits"
        )
        with connection.cursor() as cursor:
            # The fixtures' inserts leave deferred foreign-key checks pending,
            # which would block the ALTER TABLE. The migration's own updates
            # touch only the slug and queue none.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with connection.schema_editor() as editor:
            editor.add_constraint(Organization, constraint)
