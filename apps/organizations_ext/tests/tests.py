from django.test import TestCase, override_settings
from django.urls import reverse
from model_bakery import baker


class OrganizationModelTestCase(TestCase):
    def test_email(self):
        """Billing email address"""
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(user)

        # Org 1 has two users and only one of which is an owner
        user2 = baker.make("users.user")
        organization2 = baker.make("organizations_ext.Organization")
        organization2.add_user(user2)
        organization.add_user(user2)

        self.assertEqual(organization.email, user.email)
        self.assertEqual(organization.users.count(), 2)
        self.assertEqual(organization.owners.count(), 1)

    def test_slug_reserved_words(self):
        """Reserve some words for frontend routing needs"""
        word = "login"
        organization = baker.make("organizations_ext.Organization", name=word)
        self.assertNotEqual(organization.slug, word)
        organization = baker.make("organizations_ext.Organization", name=word)


class OrganizationRegistrationSettingQueryTestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.url = reverse("api:list_organizations")

    @override_settings(ENABLE_ORGANIZATION_CREATION=False)
    def test_organizations_closed_registration_first_organization_create(self):
        data = {"name": "test"}
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)


class OrganizationsFilterTestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.url = reverse("api:list_organizations")

    def test_default_ordering(self):
        organizationA = baker.make(
            "organizations_ext.Organization", name="A Organization"
        )
        organizationZ = baker.make(
            "organizations_ext.Organization", name="Z Organization"
        )
        organizationB = baker.make(
            "organizations_ext.Organization", name="B Organization"
        )
        organizationA.add_user(self.user)
        organizationB.add_user(self.user)
        organizationZ.add_user(self.user)
        res = self.client.get(self.url)
        data = res.json()
        self.assertEqual(data[0]["name"], organizationA.name)
        self.assertEqual(data[2]["name"], organizationZ.name)
