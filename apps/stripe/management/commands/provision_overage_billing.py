"""Provision the Stripe objects metered overage billing needs.

Idempotent and **test-mode only**: it refuses to run unless STRIPE_SECRET_KEY
is an ``sk_test`` key, so it can never mutate a live Stripe account. It creates
(or reuses):

1. a Billing Meter for ``GLITCHTIP_OVERAGE_METER_EVENT_NAME``;
2. an overage product (metadata ``product_type=overage``) with a graduated,
   metered price built from ``GLITCHTIP_OVERAGE_TIERS``;
3. a base "Small" hosted plan, if no hosted plan exists yet, so there's
   something to attach overage to during local end-to-end testing.

Then it syncs everything into the local DB via ``sync_stripe_models``.

``--verify`` instead runs read-only checks and is allowed with a live key: it
confirms the meter, overage product, and metered price exist, that the live
tier schedule matches ``GLITCHTIP_OVERAGE_TIERS``, and that an event
destination delivers meter error reports. Live provisioning is done by hand
(this command won't mutate a live account), and the local cap math trusts the
settings schedule — drift between the two can bill an org past its advertised
spend cap, so verify after any manual change.
"""

import json

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.stripe.client import (
    create_meter,
    create_metered_price,
    create_product,
    find_meter,
    list_prices,
    list_products,
    stripe_get,
    stripe_get_v2,
    stripe_post,
)
from apps.stripe.exceptions import StripeError
from apps.stripe.maintenance import sync_stripe_models
from apps.stripe.overage import tier_problems


async def _find_product_by_type(product_type: str):
    async for page in list_products():
        for product in page:
            if product.metadata.get("product_type", "").lower() == product_type:
                return product
    return None


async def _find_metered_prices(product_id: str, meter_id: str) -> list:
    prices = []
    async for page in list_prices():
        for price in page:
            recurring = price.recurring or {}
            if price.product == product_id and recurring.get("meter") == meter_id:
                prices.append(price)
    return prices


async def _find_metered_price(product_id: str, meter_id: str):
    prices = await _find_metered_prices(product_id, meter_id)
    return prices[0] if prices else None


async def _list_event_destinations() -> list[dict]:
    """All v2 event destinations, following pagination."""
    destinations: list[dict] = []
    endpoint = "core/event_destinations?limit=100"
    while endpoint:
        page = json.loads(await stripe_get_v2(endpoint))
        destinations += page.get("data") or []
        next_url = page.get("next_page_url") or ""
        endpoint = next_url.split("/v2/", 1)[1] if "/v2/" in next_url else ""
    return destinations


async def _verify(stdout) -> None:
    """Read-only checks that the Stripe account matches settings (live-safe)."""
    event_name = settings.GLITCHTIP_OVERAGE_METER_EVENT_NAME

    meter = await find_meter(event_name)
    if meter is None:
        raise CommandError(f"No active Billing Meter with event_name {event_name!r}.")
    stdout(f"Meter {meter.id} ({event_name})")

    product = await _find_product_by_type("overage")
    if product is None:
        raise CommandError("No product with metadata product_type=overage.")
    stdout(f"Overage product {product.id}")

    problems = []
    if product.metadata.get("is_public", "").lower() == "true":
        problems.append(
            f"Product {product.id} has is_public=true; the overage product "
            "must not appear in the public plan list."
        )

    active = [p for p in await _find_metered_prices(product.id, meter.id) if p.active]
    if not active:
        problems.append(
            f"No active metered price on product {product.id} bound to "
            f"meter {meter.id}."
        )
        raise CommandError("\n".join(problems))
    if len(active) > 1:
        # configure_overage attaches a single price chosen from the local DB;
        # verifying one while the app attaches another would hide drift.
        ids = ", ".join(p.id for p in active)
        problems.append(
            f"{len(active)} active metered prices bound to the meter ({ids}); "
            "archive all but one."
        )
    for price in active:
        stdout(f"Metered price {price.id}")
        recurring = price.recurring or {}
        if recurring.get("interval") != "month":
            problems.append(
                f"price {price.id}: interval is {recurring.get('interval')!r}, "
                "expected 'month' (local cap math assumes monthly resets)."
            )
        raw_price = json.loads(
            await stripe_get(f"prices/{price.id}", {"expand": ["tiers"]})
        )
        problems += [f"price {price.id}: {p}" for p in tier_problems(raw_price)]

    # Meter events validate asynchronously; failures only reach the webhook
    # handler if an event destination for the error report exists. Without one
    # they are silent under-billing.
    error_report = "v1.billing.meter.error_report_triggered"
    subscribed = [
        d
        for d in await _list_event_destinations()
        if d.get("status") == "enabled"
        and (
            error_report in (d.get("enabled_events") or [])
            or "*" in (d.get("enabled_events") or [])
        )
    ]
    if subscribed:
        # Print type and endpoint so the operator can eyeball that it actually
        # points at this install's /stripe/webhook/meter/.
        for dest in subscribed:
            endpoint_url = (dest.get("webhook_endpoint") or {}).get("url") or ""
            stdout(
                f"Meter error-report event destination: {dest.get('id')} "
                f"({dest.get('type')}) {endpoint_url}".rstrip()
            )
        if not settings.STRIPE_WEBHOOK_SECRET_METER:
            problems.append(
                "STRIPE_WEBHOOK_SECRET_METER is not set; deliveries to "
                "/stripe/webhook/meter/ would fail signature verification."
            )
    else:
        problems.append(
            f"No enabled event destination subscribes to {error_report}; async "
            "meter ingestion failures would go unreported. Create one pointing "
            "at /stripe/webhook/meter/ and set STRIPE_WEBHOOK_SECRET_METER to "
            "its signing secret."
        )
    if problems:
        raise CommandError("\n".join(problems))


async def _provision(stdout) -> None:
    # Defense in depth: this function creates real Stripe objects, so guard the
    # test-key check here too, not only at the command entry point.
    if not (settings.STRIPE_SECRET_KEY or "").startswith("sk_test"):
        raise CommandError("Refusing to provision against a non-test Stripe key.")

    event_name = settings.GLITCHTIP_OVERAGE_METER_EVENT_NAME

    # 1. Billing Meter
    meter = await find_meter(event_name)
    if meter is None:
        meter = await create_meter("GlitchTip Overage Events", event_name)
        stdout(f"Created meter {meter.id} ({event_name})")
    else:
        stdout(f"Reusing meter {meter.id} ({event_name})")

    # 2. Overage product + graduated metered price
    product = await _find_product_by_type("overage")
    if product is None:
        product_id = await create_product(
            "GlitchTip Overage",
            metadata={"product_type": "overage"},
            description="Usage-based charges for events above your plan quota.",
        )
        stdout(f"Created overage product {product_id}")
    else:
        product_id = product.id
        stdout(f"Reusing overage product {product_id}")

    price = await _find_metered_price(product_id, meter.id)
    if price is None:
        price = await create_metered_price(
            product_id,
            meter.id,
            settings.GLITCHTIP_OVERAGE_TIERS,
            nickname="GlitchTip overage (graduated)",
        )
        await stripe_post(f"products/{product_id}", {"default_price": price.id})
        stdout(f"Created metered overage price {price.id}")
    else:
        stdout(f"Reusing metered overage price {price.id}")

    # 3. A base hosted plan to attach overage to (for local testing only).
    hosted = await _find_product_by_type("hosted")
    if hosted is None:
        base_id = await create_product(
            "GlitchTip Small (test)",
            metadata={
                "product_type": "hosted",
                "events": "100000",
                "is_public": "true",
            },
            description="Up to 100k events per month.",
        )
        base_price = await stripe_post(
            "prices",
            {
                "product": base_id,
                "currency": "usd",
                "unit_amount": 1500,
                "recurring[interval]": "month",
                "nickname": "GlitchTip Small (test)",
                "metadata[is_public]": "true",
            },
        )
        base_price_id = json.loads(base_price)["id"]
        await stripe_post(f"products/{base_id}", {"default_price": base_price_id})
        stdout(f"Created base hosted plan {base_id} / price {base_price_id}")
    else:
        stdout(f"Found existing hosted plan {hosted.id}")

    await sync_stripe_models()
    stdout("Synced Stripe products/prices/subscriptions into the database.")


class Command(BaseCommand):
    help = (
        "Provision (idempotently) the Stripe meter/product/price for overage billing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Read-only: check the meter, overage product, and tier schedule "
            "against settings without creating anything. Safe with a live key.",
        )

    def handle(self, *args, **options):
        if options["verify"]:
            try:
                async_to_sync(_verify)(lambda msg: self.stdout.write(msg))
            except StripeError as e:
                raise CommandError(
                    f"Stripe API error (status={e.status}): {e.message}"
                ) from e
            self.stdout.write(
                self.style.SUCCESS("Overage billing configuration verified.")
            )
            return
        key = settings.STRIPE_SECRET_KEY or ""
        if not key.startswith("sk_test"):
            raise CommandError(
                "Refusing to run: STRIPE_SECRET_KEY is not a test key (sk_test...). "
                "This command must only touch a Stripe test account."
            )
        async_to_sync(_provision)(lambda msg: self.stdout.write(msg))
        self.stdout.write(self.style.SUCCESS("Overage billing provisioned."))
