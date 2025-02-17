from typing import AsyncGenerator, Type, TypeAlias, TypeVar

import aiohttp
from django.conf import settings
from pydantic import BaseModel

from .schema import (
    ProductExpandedPrice,
    ProductExpandedPriceListResponse,
    StripeListResponse,
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
) -> dict | T | list[T]:
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


async def stripe_post(endpoint: str, data: dict) -> dict:
    """Makes POST requests to the Stripe API."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{STRIPE_URL}/{endpoint}", headers=HEADERS, data=data
        ) as response:
            if response.status != 200:
                error_data = await response.json()
                raise Exception(
                    f"Stripe API Error: {response.status} - {error_data.get('error', {}).get('message', 'Unknown error')}"
                )
            return await response.json()


async def _paginated_stripe_get(
    endpoint: str,
    response_model: Type[StripeListResponse[T]],  # Use the generic type here
    params: dict[str, AIODictParams] = None,
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


async def list_subscriptions() -> AsyncGenerator[
    list[SubscriptionExpandCustomer], None
]:
    """Yield each subscription with associated price and customer"""
    params = {"expand": ["data.items.data.price", "data.customer"]}
    async for page in _paginated_stripe_get(
        "subscriptions", SubscriptionExpandCustomerResponse, params
    ):
        yield page
