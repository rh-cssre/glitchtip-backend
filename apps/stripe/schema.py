from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class StripeListResponse(BaseModel, Generic[T]):
    object: Literal["list"]
    url: str
    has_more: bool
    data: list[T]


class Product(BaseModel):
    object: Literal["product"]
    id: str
    active: bool
    attributes: list
    created: int
    default_price: str | None
    description: str | None
    images: list[str]
    livemode: bool
    marketing_features: list
    metadata: dict[str, str] | None
    name: str
    statement_descriptor: str | None
    tax_code: str | None
    type: str
    unit_label: str | None
    updated: int
    url: str | None


class Customer(BaseModel):
    object: Literal["customer"]
    id: str
    email: str
    metadata: dict[str, str] | None
    name: str | None


class Price(BaseModel):
    object: Literal["price"]
    id: str
    active: bool
    billing_scheme: str | None
    created: int
    currency: str
    livemode: bool
    lookup_key: str | None
    nickname: str | None
    product: str | dict | None  # Can be a string ID or a nested Product object
    recurring: dict | None
    tax_behavior: str | None
    tiers_mode: str | None
    type: str
    unit_amount: int | None
    unit_amount_decimal: str | None
    metadata: dict[str, str] | None


class ProductExpandedPrice(Product):
    default_price: Price | None


ProductExpandedPriceListResponse = StripeListResponse[ProductExpandedPrice]


class Items(BaseModel):
    object: Literal["list"]
    data: list[dict]


class Subscription(BaseModel):
    object: Literal["subscription"]
    id: str
    customer: str | dict | None  # Can be a string ID or a nested Customer object
    items: Items
    created: int
    current_period_end: int
    current_period_start: int
    status: str
    livemode: bool
    metadata: dict[str, str] | None
    cancel_at_period_end: bool


class SubscriptionExpandCustomer(Subscription):
    customer: Customer


SubscriptionExpandCustomerResponse = StripeListResponse[SubscriptionExpandCustomer]


class EventData(BaseModel):
    object: Product | Price | Subscription = Field(discriminator="object")
    previous_attributes: dict | None


class StripeEvent(BaseModel):
    id: str
    object: str = "event"  # Ensure it is an event
    api_version: str
    created: int
    data: EventData
    livemode: bool
    pending_webhooks: int
    request: dict  # Or a more specific Request model if needed
    type: str  # This field is crucial for discrimination
