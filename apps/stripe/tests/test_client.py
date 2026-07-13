import json
from unittest.mock import patch

from aioresponses import aioresponses
from django.test import TestCase

from apps.stripe.client import STRIPE_URL, stripe_get, stripe_post
from apps.stripe.exceptions import StripeResourceNotFound


class StripeClientRetryTests(TestCase):
    def setUp(self):
        self.sleep_patcher = patch("apps.stripe.client.asyncio.sleep")
        self.mock_sleep = self.sleep_patcher.start()

        async def _no_sleep(_):
            return None

        self.mock_sleep.side_effect = _no_sleep
        self.addCleanup(self.sleep_patcher.stop)

    async def test_stripe_get_retries_on_429_should_retry(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            mocked.get(
                url,
                status=429,
                payload={"error": {"message": "lock_timeout"}},
                headers={"Stripe-Should-Retry": "true"},
            )
            mocked.get(url, status=200, body='{"ok": true}')
            body = await stripe_get("customers/cus_test")
        self.assertEqual(body, '{"ok": true}')

    async def test_stripe_get_retries_on_5xx_without_header(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            mocked.get(url, status=503, payload={"error": {"message": "unavailable"}})
            mocked.get(url, status=200, body='{"ok": true}')
            body = await stripe_get("customers/cus_test")
        self.assertEqual(body, '{"ok": true}')

    async def test_stripe_get_does_not_retry_when_header_says_false(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            mocked.get(
                url,
                status=429,
                payload={"error": {"message": "do not retry"}},
                headers={"Stripe-Should-Retry": "false"},
            )
            with self.assertRaises(Exception) as ctx:
                await stripe_get("customers/cus_test")
        self.assertIn("429", str(ctx.exception))

    async def test_stripe_get_raises_after_exhausting_retries(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            for _ in range(4):
                mocked.get(
                    url,
                    status=429,
                    payload={"error": {"message": "still locked"}},
                    headers={"Stripe-Should-Retry": "true"},
                )
            with self.assertRaises(Exception) as ctx:
                await stripe_get("customers/cus_test")
        self.assertIn("429", str(ctx.exception))

    async def test_stripe_get_raises_resource_not_found_without_retry(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            mocked.get(url, status=404, payload={"error": {"message": "missing"}})
            with self.assertRaises(StripeResourceNotFound):
                await stripe_get("customers/cus_test")

    async def test_stripe_post_retries_on_429(self):
        url = f"{STRIPE_URL}/customers"
        with aioresponses() as mocked:
            mocked.post(
                url,
                status=429,
                payload={"error": {"message": "lock_timeout"}},
                headers={"Stripe-Should-Retry": "true"},
            )
            mocked.post(url, status=200, body=json.dumps({"id": "cus_new"}))
            body = await stripe_post("customers", {"name": "x"})
        self.assertEqual(json.loads(body)["id"], "cus_new")
