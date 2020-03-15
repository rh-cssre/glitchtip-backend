from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from model_bakery.random_gen import gen_slug, gen_datetime, gen_integer
from glitchtip import test_utils  # pylint: disable=unused-import


baker.generators.add("djstripe.fields.StripeIdField", gen_slug)
baker.generators.add("djstripe.fields.StripeDateTimeField", gen_datetime)
baker.generators.add("djstripe.fields.StripeQuantumCurrencyAmountField", gen_integer)


class SubscriptionAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user)
        self.client.force_login(self.user)
        self.url = reverse("subscription-list")

    def test_list(self):
        customer = baker.make("djstripe.Customer", subscriber=self.organization)
        subscription = baker.make(
            "djstripe.Subscription", customer=customer, livemode=False
        )

        subscription2 = baker.make("djstripe.Subscription", livemode=False)
        subscription3 = baker.make(
            "djstripe.Subscription", customer=customer, livemode=True
        )

        res = self.client.get(self.url)
        self.assertContains(res, subscription.id)
        self.assertNotContains(res, subscription2.id)
        self.assertNotContains(res, subscription3.id)

    def test_detail(self):
        customer = baker.make("djstripe.Customer", subscriber=self.organization)
        subscription = baker.make(
            "djstripe.Subscription", customer=customer, livemode=False
        )
        baker.make("djstripe.Subscription")
        url = reverse("subscription-detail", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, subscription.id)
