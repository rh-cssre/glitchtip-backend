import asyncio
import json
import random
import re
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import aiohttp
from django.conf import settings
from pydantic import BaseModel

from apps.organizations_ext.models import Organization

from .exceptions import StripeError, StripeResourceNotFound
from .schema import (
    Customer,
    Meter,
    MeterEvent,
    MeterListResponse,
    PortalSession,
    Price,
    PriceListResponse,
    ProductExpandedPrice,
    ProductExpandedPriceListResponse,
    Session,
    StripeListResponse,
    Subscription,
    SubscriptionExpandCustomer,
    SubscriptionExpandCustomerResponse,
    SubscriptionItem,
    SubscriptionListResponse,
    ThinEvent,
)

STRIPE_URL = "https://api.stripe.com/v1"
STRIPE_V2_URL = "https://api.stripe.com/v2"
HEADERS = {
    "Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
    "Stripe-Version": "2025-12-15.clover",
}

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_RETRY_DELAY = 0.5
# Stripe calls run inline in worker tasks (the throttle sweep reports overage
# per org sequentially, webhook handling fetches related objects), so bound them
# tighter than the global 30s default: a slow Stripe response must not stall a
# sweep. Per-request, so it overrides AIOHTTP_CONFIG's session default. This is
# per attempt; _stripe_request retries up to MAX_RETRIES on 429/5xx.
STRIPE_TIMEOUT = aiohttp.ClientTimeout(total=10)

type AIOTupleParams = list[tuple[str, str]]
type AIODictParams = dict[str, int | str | list[int | str]]


def param_helper(data: AIODictParams) -> AIOTupleParams:
    """Accept {foo: [1,2]} format and convert aio-friendly to list of tuples"""
    params: AIOTupleParams = []
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                params.append((f"{key}[]", str(item)))
        else:
            params.append((key, str(value)))
    return params


async def _stripe_request(
    method: str, url: str, max_retries: int = MAX_RETRIES, **kwargs: Any
) -> str:
    """Issue a Stripe API request, retrying transient failures with backoff.

    Honors Stripe's ``Stripe-Should-Retry`` response header when present; otherwise
    retries on 429 and 5xx. Each attempt opens its own ``ClientSession`` to avoid
    reusing a connection that may have been poisoned by the prior failure.
    """
    for attempt in range(max_retries + 1):
        async with (
            aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session,
            session.request(
                method, url, headers=HEADERS, timeout=STRIPE_TIMEOUT, **kwargs
            ) as response,
        ):
            if response.status == 200:
                return await response.text()
            if response.status == 404:
                raise StripeResourceNotFound()

            error = (await response.json()).get("error", {})

            should_retry_header = response.headers.get("Stripe-Should-Retry")
            if should_retry_header is not None:
                should_retry = should_retry_header.lower() == "true"
            else:
                should_retry = response.status in RETRY_STATUSES

            if not should_retry or attempt >= max_retries:
                raise StripeError(
                    error.get("message", "Unknown error"),
                    status=response.status,
                    type=error.get("type", ""),
                    code=error.get("code", ""),
                )

        delay = BASE_RETRY_DELAY * (2**attempt) + random.uniform(0, BASE_RETRY_DELAY)
        await asyncio.sleep(delay)

    raise StripeError("exhausted retries", status=503)


async def stripe_get(
    endpoint: str,
    params: AIODictParams | AIOTupleParams | None = None,
) -> str:
    """Makes GET requests to the Stripe API."""
    if isinstance(params, dict):
        params = param_helper(params)
    return await _stripe_request("GET", f"{STRIPE_URL}/{endpoint}", params=params)


async def stripe_post(endpoint: str, data: dict) -> str:
    """Makes POST requests to the Stripe API. Returns response text"""
    return await _stripe_request("POST", f"{STRIPE_URL}/{endpoint}", data=data)


async def stripe_delete(endpoint: str) -> str:
    """Makes DELETE requests to the Stripe API. Returns response text"""
    return await _stripe_request("DELETE", f"{STRIPE_URL}/{endpoint}")


async def _paginated_stripe_get[T: BaseModel](
    endpoint: str,
    response_model: type[StripeListResponse[T]],  # Use the generic type here
    params: dict[str, AIODictParams] | None = None,
) -> AsyncGenerator[list[T], None]:
    """
    Generic function to handle paginated GET requests to the Stripe API.

    Args:
        endpoint: The Stripe API endpoint (e.g., "products", "subscriptions").
        response_model: The Pydantic model for the *entire* response (including has_more and data).
        params:  Initial query parameters.  These will be *updated* with pagination parameters.

    Yields:
        Lists of the data objects from each page.
    """

    has_more = True
    starting_after: str | None = None
    # Create a copy of the params to avoid modifying the original
    local_params = params.copy() if params else {}
    local_params["limit"] = 100  # Consistent limit

    while has_more:
        if starting_after:
            local_params["starting_after"] = starting_after

        result = await stripe_get(endpoint, params=local_params)
        response = response_model.model_validate_json(result)

        has_more = response.has_more
        if has_more and response.data:
            starting_after = response.data[-1].id
        yield response.data


async def list_products() -> AsyncGenerator[list[ProductExpandedPrice], None]:
    """Yield each page of products with associated default price.

    Archived products are included so the sync can record ``active=False``
    rather than infer it from absence — absence now means the product was
    deleted in Stripe, or its metadata no longer qualifies it for sync.
    """
    params = {"expand": ["data.default_price"]}
    async for page in _paginated_stripe_get(
        "products", ProductExpandedPriceListResponse, params
    ):
        yield page


async def list_subscriptions() -> AsyncGenerator[
    list[SubscriptionExpandCustomer], None
]:
    """Yield each subscription with associated price and customer"""
    params = {"expand": ["data.customer"]}
    async for page in _paginated_stripe_get(
        "subscriptions", SubscriptionExpandCustomerResponse, params
    ):
        yield page


async def list_prices() -> AsyncGenerator[list[Price], None]:
    """Yield each price"""
    async for page in _paginated_stripe_get("prices", PriceListResponse):
        yield page


async def create_customer(organization: Organization) -> Customer:
    """
    Create a Stripe customer for the given organization, saving the customer ID
    to the organization.
    """
    response = await stripe_post(
        "customers",
        {
            "name": organization.name,
            "email": organization.email,
            "metadata[organization_id]": organization.id,
            "metadata[organization_slug]": organization.slug,
            "metadata[region]": settings.STRIPE_REGION,
        },
    )
    customer = Customer.model_validate_json(response)
    organization.stripe_customer_id = customer.id
    await organization.asave(update_fields=["stripe_customer_id"])
    return customer


async def create_session(
    price_id: str, customer_id: str, organization_slug: str
) -> Session:
    domain = settings.GLITCHTIP_URL.geturl()
    params = {
        "payment_method_types[]": "card",
        "line_items[][price]": price_id,
        "line_items[][quantity]": 1,
        "mode": "subscription",
        "customer": customer_id,
        "automatic_tax[enabled]": True,
        "customer_update[address]": "auto",
        "customer_update[name]": "auto",
        "tax_id_collection[enabled]": True,
        # Set explicitly rather than relying on the Stripe-Version default, so
        # the mode a subscription is born with never silently changes with a
        # version bump. See create_subscription for what flexible mode buys us.
        "subscription_data[billing_mode][type]": "flexible",
        "success_url": domain
        + "/"
        + organization_slug
        + "/settings/subscription?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": domain + "/" + organization_slug + "/settings/subscription",
    }
    response = await stripe_post("checkout/sessions", params)
    return Session.model_validate_json(response)


async def create_portal_session(customer_id: str, organization_slug: str):
    domain = settings.GLITCHTIP_URL.geturl()
    params = {
        "customer": customer_id,
        "return_url": domain
        + "/"
        + organization_slug
        + "/settings/subscription?billing_portal_redirect=true",
    }
    response = await stripe_post("billing_portal/sessions", params)
    return PortalSession.model_validate_json(response)


async def mark_welcome_sent(subscription_id: str) -> None:
    """Record on the Stripe subscription that the support-license welcome email
    was sent. The bracket form merges the key, leaving other metadata intact."""
    await stripe_post(
        f"subscriptions/{subscription_id}", {"metadata[welcome_sent]": "true"}
    )


async def create_subscription(customer: str, price: str, **kwargs) -> Subscription:
    """Create a subscription in flexible billing mode.

    Flexible mode is a prerequisite for attaching a metered price, and it stops
    Stripe from resetting the billing cycle anchor on plan changes — the anchor
    drives the quota window an organization is throttled against. Set
    explicitly rather than relying on the Stripe-Version default, which differs
    across versions.
    """
    params = {
        "customer": customer,
        "items[][price]": price,
        "billing_mode[type]": "flexible",
        **kwargs,
    }
    response = await stripe_post("subscriptions", params)
    return Subscription.model_validate_json(response)


async def list_customer_subscriptions(customer_id: str) -> list[Subscription]:
    """Every non-canceled subscription of ``customer_id``, across pages.

    Stripe's default status filter omits canceled and incomplete_expired, which
    is the set that can still carry a billable metered item.
    """
    subscriptions: list[Subscription] = []
    async for page in _paginated_stripe_get(
        "subscriptions", SubscriptionListResponse, {"customer": customer_id}
    ):
        subscriptions += page
    return subscriptions


async def fetch_subscription(id: str) -> Subscription:
    response = await stripe_get("subscriptions/" + id)
    return Subscription.model_validate_json(response)


async def cancel_subscription(id: str) -> Subscription:
    response = await stripe_delete("subscriptions/" + id)
    return Subscription.model_validate_json(response)


# --- Metered (overage) billing ---------------------------------------------
# Modern usage-based billing: a Billing Meter aggregates reported usage events
# per customer, and a tiered metered Price attached to it as a second
# subscription item turns that usage into invoice line items.


async def find_meter(event_name: str) -> Meter | None:
    """Return the active Billing Meter for ``event_name``, if one exists."""
    response = await stripe_get("billing/meters", {"status": "active", "limit": 100})
    for meter in MeterListResponse.model_validate_json(response).data:
        if meter.event_name == event_name:
            return meter
    return None


async def create_meter(display_name: str, event_name: str) -> Meter:
    """Create a last-value Billing Meter keyed by customer id.

    Usage events carry ``payload[stripe_customer_id]`` and ``payload[value]``,
    where ``value`` is the customer's cycle-to-date billable total; Stripe bills
    the last value received in each billing period. Reports are therefore
    idempotent -- a retry, a concurrent sweep, or a replacement subscription all
    restate the same figure -- which is what lets the throttle task keep no
    delta counter. A ``sum`` meter fed the same reports would compound them.
    """
    response = await stripe_post(
        "billing/meters",
        {
            "display_name": display_name,
            "event_name": event_name,
            "default_aggregation[formula]": "last",
            "customer_mapping[type]": "by_id",
            "customer_mapping[event_payload_key]": "stripe_customer_id",
            "value_settings[event_payload_key]": "value",
        },
    )
    return Meter.model_validate_json(response)


async def create_product(
    name: str, metadata: dict[str, str] | None = None, description: str = ""
) -> str:
    """Create a Stripe product, returning its id."""
    data: dict = {"name": name}
    if description:
        data["description"] = description
    for key, value in (metadata or {}).items():
        data[f"metadata[{key}]"] = value
    response = await stripe_post("products", data)
    return json.loads(response)["id"]


async def create_metered_price(
    product_id: str,
    meter_id: str,
    tiers: list[tuple[int | None, str]],
    nickname: str = "",
    interval: str = "month",
    currency: str = "usd",
) -> Price:
    """Create a graduated, metered Price backed by ``meter_id``.

    ``tiers`` is the canonical ``(up_to_units, per_unit_usd_decimal)`` schedule
    from settings; the final tier uses ``up_to=None`` for "and beyond". Stripe's
    ``unit_amount_decimal`` is in the currency's minor unit (cents), so per-event
    USD is multiplied by 100.
    """
    data: dict = {
        "product": product_id,
        "currency": currency,
        "billing_scheme": "tiered",
        "tiers_mode": "graduated",
        "recurring[interval]": interval,
        "recurring[usage_type]": "metered",
        "recurring[meter]": meter_id,
    }
    if nickname:
        data["nickname"] = nickname
    for i, (up_to, rate) in enumerate(tiers):
        data[f"tiers[{i}][up_to]"] = "inf" if up_to is None else str(up_to)
        data[f"tiers[{i}][unit_amount_decimal]"] = str(Decimal(rate) * 100)
    response = await stripe_post("prices", data)
    return Price.model_validate_json(response)


async def add_subscription_item(
    subscription_id: str, price_id: str
) -> SubscriptionItem:
    """Attach ``price_id`` to a subscription as an additional item."""
    response = await stripe_post(
        "subscription_items",
        {"subscription": subscription_id, "price": price_id},
    )
    return SubscriptionItem.model_validate_json(response)


async def stripe_get_v2(endpoint: str, max_retries: int = MAX_RETRIES) -> str:
    """Makes GET requests to the Stripe v2 API (events, event destinations)."""
    return await _stripe_request(
        "GET", f"{STRIPE_V2_URL}/{endpoint}", max_retries=max_retries
    )


async def fetch_v2_event(event_id: str) -> ThinEvent:
    """Fetch a v2 event by id; thin webhook payloads omit the event details.

    Runs inline in a webhook response, so a single attempt only: Stripe's
    delivery timeout is shorter than one local retry cycle, and its own
    redelivery is the retry mechanism. The id lands in the URL path, so its
    shape is validated as defense in depth.
    """
    if not re.fullmatch(r"evt_\w+", event_id):
        raise ValueError("Invalid v2 event id")
    response = await stripe_get_v2(f"core/events/{event_id}", max_retries=0)
    return ThinEvent.model_validate_json(response)


async def create_meter_event(
    event_name: str,
    customer_id: str,
    value: int,
    identifier: str,
    timestamp: int | None = None,
) -> MeterEvent:
    """Report usage to a Billing Meter.

    ``timestamp`` places the event in a billing period; ``identifier`` must be
    unique per event within Stripe's dedupe window, or the event is dropped.
    """
    data: dict = {
        "event_name": event_name,
        "payload[stripe_customer_id]": customer_id,
        "payload[value]": str(value),
        "identifier": identifier,
    }
    if timestamp is not None:
        data["timestamp"] = timestamp
    response = await stripe_post("billing/meter_events", data)
    return MeterEvent.model_validate_json(response)
