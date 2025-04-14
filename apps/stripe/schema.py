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
    metadata: dict[str, str]
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
    email: str | None
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
    product: str | dict | None
    recurring: dict | None
    tax_behavior: str | None
    tiers_mode: str | None
    type: str
    unit_amount: int | None
    unit_amount_decimal: str | None
    metadata: dict[str, str] | None


PriceListResponse = StripeListResponse[Price]


class ProductExpandedPrice(Product):
    default_price: Price | None


ProductExpandedPriceListResponse = StripeListResponse[ProductExpandedPrice]


class SubscriptionItem(BaseModel):
    id: str
    object: Literal["subscription_item"]
    created: int
    current_period_end: int
    current_period_start: int
    metadata: dict[str, str]
    price: Price
    quantity: int
    subscription: str
    tax_rates: list


class SubscriptionItems(BaseModel):
    object: Literal["list"]
    data: list[SubscriptionItem]


class SubscriptionBase(BaseModel):
    object: Literal["subscription"]
    id: str
    items: SubscriptionItems
    created: int
    status: str
    livemode: bool
    metadata: dict[str, str] | None
    cancel_at_period_end: bool
    start_date: int
    collection_method: str


class Subscription(SubscriptionBase):
    customer: str


class SubscriptionExpandCustomer(SubscriptionBase):
    customer: Customer


SubscriptionExpandCustomerResponse = StripeListResponse[SubscriptionExpandCustomer]


class EventData(BaseModel):
    object: Product | Price | Subscription = Field(discriminator="object")
    previous_attributes: dict | None = None


class StripeEventRequest(BaseModel):
    id: str | None = None
    idempotency_key: str | None = None


class StripeEvent(BaseModel):
    id: str
    object: str = "event"
    api_version: str
    created: int
    data: EventData
    livemode: bool
    pending_webhooks: int
    request: StripeEventRequest
    type: str


class AutomaticTax(BaseModel):
    enabled: bool
    liability: dict | None = None
    status: str | None = None


class CustomText(BaseModel):
    shipping_address: str | None = None
    submit: str | None = None


class InvoiceData(BaseModel):
    account_tax_ids: list[str] | None = None
    custom_fields: list[str] | None = None
    description: str | None = None
    footer: str | None = None
    issuer: str | None = None
    metadata: dict[str, str]
    rendering_options: str | None = None


class InvoiceCreation(BaseModel):
    enabled: bool
    invoice_data: InvoiceData


class PhoneNumberCollection(BaseModel):
    enabled: bool


class TotalDetails(BaseModel):
    amount_discount: int
    amount_shipping: int
    amount_tax: int


class Session(BaseModel):
    id: str
    object: str
    after_expiration: str | None = None
    allow_promotion_codes: str | None = None
    amount_subtotal: int
    amount_total: int
    automatic_tax: AutomaticTax
    billing_address_collection: str | None = None
    cancel_url: str | None = None
    client_reference_id: str | None = None
    consent: str | None = None
    consent_collection: str | None = None
    created: int
    currency: str
    custom_fields: list
    custom_text: CustomText
    customer: str | None = None
    customer_creation: str | None
    customer_details: dict | None = None
    customer_email: str | None = None
    expires_at: int
    invoice: str | None = None
    invoice_creation: InvoiceCreation | None
    livemode: bool
    locale: str | None = None
    metadata: dict[str, str]
    mode: str
    payment_intent: str | None = None
    payment_link: str | None = None
    payment_method_collection: str
    payment_method_options: dict
    payment_method_types: list[str]
    payment_status: str
    phone_number_collection: PhoneNumberCollection
    recovered_from: str | None = None
    setup_intent: str | None = None
    shipping_address_collection: str | None = None
    shipping_cost: str | None = None
    shipping_details: str | None = None
    shipping_options: list
    status: str
    submit_type: str | None = None
    subscription: str | None = None
    success_url: str
    total_details: TotalDetails
    url: str


class PortalSession(BaseModel):
    id: str
    object: Literal["billing_portal.session"]
    configuration: str
    created: int
    customer: str
    flow: str | None = None
    livemode: bool
    locale: str | None = None
    on_behalf_of: str | None = None
    return_url: str
    url: str
