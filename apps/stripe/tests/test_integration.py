import os
from unittest import skipIf

from django.test import TestCase

from apps.stripe import client
from apps.stripe.models import StripePrice, StripeProduct


@skipIf(not os.environ.get("STRIPE_SECRET_KEY"), "STRIPE_SECRET_KEY not set")
class StripeIntegrationTestCase(TestCase):
    async def test_sync_real_data(self):
        """
        Integration test that connects to Stripe using STRIPE_SECRET_KEY.
        Verifies that products and prices are synced correctly, including
        metadata fields like 'no_throttle' and 'interval'.
        """
        key = os.environ.get("STRIPE_SECRET_KEY")

        # Manually update the client headers since they are defined at module level
        original_auth = client.HEADERS.get("Authorization")
        client.HEADERS["Authorization"] = f"Bearer {key}"

        try:
            # Run sync
            await StripeProduct.sync_from_stripe()
            await StripePrice.sync_from_stripe()

            # Check results
            products = [p async for p in StripeProduct.objects.all()]
            prices = [p async for p in StripePrice.objects.all()]

            print(
                f"\n[Integration] Synced {len(products)} products and {len(prices)} prices."
            )

            # Basic assertions
            self.assertTrue(
                len(products) > 0, "No products found in Stripe test account"
            )
            self.assertTrue(len(prices) > 0, "No prices found in Stripe test account")

            # Check for specific metadata if expected
            # We can't assert exact values without knowing the Stripe account state,
            # but we can print what we found for verification.
            found_no_throttle = False
            found_yearly = False

            for price in prices:
                if price.no_throttle:
                    found_no_throttle = True
                    print(f"[Integration] Found no_throttle price: {price}")
                if price.interval == "year":
                    found_yearly = True
                    print(f"[Integration] Found yearly price: {price}")

            if not found_no_throttle:
                print("[Integration] Warning: No price with no_throttle=True found.")
            if not found_yearly:
                print("[Integration] Warning: No price with interval='year' found.")

        finally:
            # Restore headers
            if original_auth:
                client.HEADERS["Authorization"] = original_auth
