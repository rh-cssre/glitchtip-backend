from typing import AsyncGenerator, Type, TypeAlias, TypeVar

import aiohttp
from django.conf import settings
from pydantic import BaseModel

from apps.organizations_ext.models import Organization

from .schema import (
    Customer,
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
)

STRIPE_URL = "https://api.stripe.com/v1"
HEADERS = {
    "Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
    "Content-Type": "application/x-www-form-urlencoded",
    "Stripe-Version": "2025-01-27.acacia",
}

AIOTupleParams: TypeAlias = list[tuple[str, str]]
AIODictParams: TypeAlias = dict[str, int | str | list[int | str]]
T = TypeVar("T", bound=BaseModel)


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


async def stripe_get(
    endpoint: str,
    params: AIODictParams | AIOTupleParams | None = None,
) -> str:
    """Makes GET requests to the Stripe API."""
    if isinstance(params, dict):
        params = param_helper(params)

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{STRIPE_URL}/{endpoint}", headers=HEADERS, params=params
        ) as response:
            if response.status != 200:
                error_data = await response.json()
                raise Exception(
                    f"Stripe API Error: {response.status} - {error_data.get('error', {}).get('message', 'Unknown error')}"
                )
            return await response.text()


async def stripe_post(endpoint: str, data: dict) -> str:
    """Makes POST requests to the Stripe API. Returns response text"""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{STRIPE_URL}/{endpoint}", headers=HEADERS, data=data
        ) as response:
            if response.status != 200:
                error_data = await response.json()
                raise Exception(
                    f"Stripe API Error: {response.status} - {error_data.get('error', {}).get('message', 'Unknown error')}"
                )
            return await response.text()


async def _paginated_stripe_get(
    endpoint: str,
    response_model: Type[StripeListResponse[T]],  # Use the generic type here
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
    """Yield each page of products with associated default price"""
    params = {"active": "true", "expand": ["data.default_price"]}
    async for page in _paginated_stripe_get(
        "products", ProductExpandedPriceListResponse, params
    ):
        yield page


async def list_subscriptions() -> (
    AsyncGenerator[list[SubscriptionExpandCustomer], None]
):
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
        "success_url": domain
        + "/"
        + organization_slug
        + "/settings/subscription?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": domain + "",
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


async def create_subscription(customer: str, price: str) -> Subscription:
    params = {"customer": customer, "items[][price]": price}
    response = await stripe_post("subscriptions", params)
    return Subscription.model_validate_json(response)
