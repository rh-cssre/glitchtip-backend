from allauth.socialaccount.models import SocialApp
from django import db
from django.contrib.auth import get_user_model
from django.test import TestCase
from model_bakery import baker


class OrganizationSocialAppTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make(get_user_model())
        cls.socialApp1 = baker.make(SocialApp)
        cls.socialApp2 = baker.make(SocialApp)
        cls.organization1 = baker.make("organizations_ext.Organization")
        cls.organization2 = baker.make("organizations_ext.Organization")
        cls.organization_social_app1 = baker.make(
            "organizations_ext.OrganizationSocialApp",
            organization=cls.organization1,
            social_app=cls.socialApp1,
        )
        cls.organization_social_app2 = baker.make(
            "organizations_ext.OrganizationSocialApp",
            organization=cls.organization1,
            social_app=cls.socialApp2,
        )

    def test_organization_social_app_association(self):
        retrieved_organization_socialapps = (
            self.organization1.organizationsocialapp_set.all()
        )
        self.assertEqual(retrieved_organization_socialapps.count(), 2)
        self.assertIn(self.organization_social_app1, retrieved_organization_socialapps)
        self.assertIn(self.organization_social_app2, retrieved_organization_socialapps)

    def test_social_app_organization_association(self):
        with self.assertRaises(db.utils.IntegrityError) as integrity_error:
            baker.make(
                "organizations_ext.OrganizationSocialApp",
                organization=self.organization2,
                social_app=self.socialApp1,
            )
        self.assertIn("unique constraint", str(integrity_error.exception))
