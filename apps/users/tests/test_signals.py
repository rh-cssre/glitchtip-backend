from allauth.account.signals import user_logged_in
from allauth.socialaccount.models import (
    SocialAccount,
    SocialApp,
)
from django.test import RequestFactory, TestCase
from model_bakery import baker

from apps.users.models import User


class TestAddUserToSocialAppOragnizationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization_1 = baker.make("organizations_ext.Organization")
        cls.organization_2 = baker.make("organizations_ext.Organization")
        cls.organization_3 = baker.make("organizations_ext.Organization")
        cls.user = baker.make("users.user")
        social_app_1 = baker.make(SocialApp, provider="google")
        social_app_2 = baker.make(SocialApp, provider="facebook", provider_id="fb")
        social_app_3 = baker.make(
            SocialApp,
            provider="openid_connect",
            provider_id="my-openid",
            settings={"server_url": "https://example.com"},
        )
        baker.make(
            "organizations_ext.OrganizationSocialApp",
            organization=cls.organization_1,
            social_app=social_app_1,
        )
        baker.make(
            "organizations_ext.OrganizationSocialApp",
            organization=cls.organization_2,
            social_app=social_app_2,
        )
        baker.make(
            "organizations_ext.OrganizationSocialApp",
            organization=cls.organization_3,
            social_app=social_app_3,
        )
        baker.make(SocialAccount, user=cls.user, provider="google")
        baker.make(SocialAccount, user=cls.user, provider="fb")
        baker.make(SocialAccount, user=cls.user, provider="my-openid")

        cls.request = RequestFactory().get("/")

    def test_user_is_added_to_all_orgs_associated_to_their_social_apps(self):
        with self.assertNumQueries(11):
            user_logged_in.send(sender=User, request=self.request, user=self.user)
        assert self.user in self.organization_1.users.all()
        assert self.user in self.organization_2.users.all()
        assert self.user in self.organization_3.users.all()

    def test_user_is_not_added_to_orgs_not_associated_to_their_social_apps(self):
        organization_4 = baker.make("organizations_ext.Organization")
        user_logged_in.send(sender=User, request=self.request, user=self.user)
        assert self.user not in organization_4.users.all()

    def test_user_is_added_to_populated_orgs_associated_to_their_social_apps(self):
        user2 = baker.make("users.user")
        self.organization_1.add_user(user2)
        user_logged_in.send(sender=User, request=self.request, user=self.user)
        assert self.user in self.organization_1.users.all()

    def test_global_social_apps(self):
        social_app = baker.make(SocialApp, provider="microsoft")
        baker.make(SocialAccount, user=self.user, provider=social_app.provider)
        user_logged_in.send(sender=User, request=self.request, user=self.user)
