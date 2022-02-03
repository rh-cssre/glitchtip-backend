from django.conf import settings
from django.shortcuts import reverse
from django.test import TestCase, RequestFactory
from rest_framework.test import APITestCase
from model_bakery import baker
from organizations_ext.models import OrganizationUser, OrganizationUserRole
from glitchtip import test_utils  # pylint: disable=unused-import


class OrganizationModelTestCase(TestCase):
    def test_email(self):
        """ Billing email address """
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

    def test_organization_request_callback(self):
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(user)

        factory = RequestFactory()
        request = factory.get("/")
        request.user = user

        callback = settings.DJSTRIPE_SUBSCRIBER_MODEL_REQUEST_CALLBACK
        self.assertEqual(callback(request), organization)

    def test_slug_reserved_words(self):
        """ Reserve some words for frontend routing needs """
        word = "login"
        organization = baker.make("organizations_ext.Organization", name=word)
        self.assertNotEqual(organization.slug, word)
        organization = baker.make("organizations_ext.Organization", name=word)


class OrganizationsAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user)
        self.client.force_login(self.user)
        self.url = reverse("organization-list")

    def test_organizations_list(self):
        not_my_organization = baker.make("organizations_ext.Organization")
        res = self.client.get(self.url)
        self.assertContains(res, self.organization.slug)
        self.assertNotContains(res, not_my_organization.slug)
        self.assertFalse(
            "teams" in res.data[0].keys(), "List view shouldn't contain teams"
        )

    def test_organizations_retrieve(self):
        project = baker.make("projects.Project", organization=self.organization)
        url = reverse("organization-detail", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, self.organization.name)
        self.assertContains(res, project.name)
        self.assertTrue(
            "teams" in res.data.keys(), "Retrieve view should contain teams"
        )

    def test_organizations_create(self):
        data = {"name": "test"}
        res = self.client.post(self.url, data)
        self.assertContains(res, data["name"], status_code=201)
        self.assertEqual(
            OrganizationUser.objects.filter(organization__name=data["name"]).count(), 1
        )

    def test_organizations_create_closed_registration(self):
        data = {"name": "test"}

        with self.settings(ENABLE_OPEN_USER_REGISTRATION=False):
            res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 201)

    def test_organizations_create_closed_registration_failure(self):
        data = {"name": "test"}
        self.organization.organization_users.all().update(
            role=OrganizationUserRole.MANAGER
        )

        with self.settings(ENABLE_OPEN_USER_REGISTRATION=False):
            res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 403)

        self.user.is_superuser = True
        self.user.save()

        with self.settings(ENABLE_OPEN_USER_REGISTRATION=False):
            res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 201)
