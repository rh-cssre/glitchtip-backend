from django.http import JsonResponse
from django.shortcuts import aget_object_or_404
from ninja import ModelSchema, Router

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.schema import CamelSchema

from .client import (
    create_customer,
    create_portal_session,
    create_session,
    create_subscription,
)
from .models import StripePrice, StripeProduct, StripeSubscription
from .utils import unix_to_datetime

router = Router()


class StripeProductSchema(CamelSchema, ModelSchema):
    class Meta:
        model = StripeProduct
        fields = ["stripe_id", "name", "description", "events"]


class StripeSubscriptionSchema(CamelSchema, ModelSchema):
    class Meta:
        model = StripeSubscription
        fields = ["stripe_id", "created", "current_period_start", "current_period_end"]


class PriceIDSchema(CamelSchema):
    price: str


class SubscriptionIn(PriceIDSchema):
    organization: int


class CreateSubscriptionResponse(SubscriptionIn):
    subscription: StripeSubscriptionSchema


@router.get("products/", response=list[StripeProductSchema])
async def list_stripe_products(request: AuthHttpRequest):
    return [
        product
        async for product in StripeProduct.objects.filter(is_public=True, events__gt=0)
    ]


@router.get(
    "subscriptions/{slug:organization_slug}/", response=StripeSubscriptionSchema | None
)
async def get_stripe_subscription(request: AuthHttpRequest, organization_slug: str):
    return await (
        StripeSubscription.objects.filter(
            organization__users=request.auth.user_id,
            organization__slug=organization_slug,
            is_active=True,
        )
        .select_related("price__product")
        .order_by("-created")
        .afirst()
    )


@router.post(
    "organizations/{slug:organization_slug}/create-stripe-subscription-checkout/"
)
async def create_stripe_session(
    request: AuthHttpRequest, organization_slug: str, payload: PriceIDSchema
):
    """
    Create Stripe Checkout, send to client for redirecting to Stripe
    See https://stripe.com/docs/api/checkout/sessions/create
    """
    organization = await aget_object_or_404(
        Organization.objects.select_related("owner__organization_user__user"),
        slug=organization_slug,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    # Ensure price exists
    price_id = payload.price
    await aget_object_or_404(StripePrice, stripe_id=price_id)
    return await create_session(price_id, customer_id, organization_slug)


@router.post("organizations/{slug:organization_slug}/create-billing-portal/")
async def stripe_billing_portal_session(
    request: AuthHttpRequest, organization_slug: str
):
    """See https://stripe.com/docs/billing/subscriptions/integrating-self-serve-portal"""
    organization = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    return await create_portal_session(customer_id, organization_slug)


@router.post("subscriptions/", response=CreateSubscriptionResponse)
async def stripe_create_subscription(request: AuthHttpRequest, payload: SubscriptionIn):
    organization = await aget_object_or_404(
        Organization,
        id=payload.organization,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    price = await aget_object_or_404(StripePrice, stripe_id=payload.price, price=0)
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    if await StripeSubscription.objects.filter(
        organization=organization, is_active=True
    ).aexists():
        return JsonResponse(
            {"detail": "Customer already has subscription"}, status_code=400
        )
    subscription_resp = await create_subscription(customer_id, price.stripe_id)
    subscription = await StripeSubscription.objects.acreate(
        is_active=True,
        created=unix_to_datetime(subscription_resp.created),
        current_period_start=unix_to_datetime(subscription_resp.current_period_start),
        current_period_end=unix_to_datetime(subscription_resp.current_period_end),
        price=price,
        organization=organization,
    )
    organization.stripe_primary_subscription = subscription
    await organization.asave(update_fields=["stripe_primary_subscription"])
    return {
        "price": price.stripe_id,
        "organization": organization.id,
        "subscription": subscription,
    }


@router.get("subscriptions/{slug:organization_slug}/events_count/")
async def subscription_events_count(request: AuthHttpRequest, organization_slug: str):
    org = await aget_object_or_404(
        Organization.objects.with_event_counts(),
        slug=organization_slug,
        users=request.auth.user_id,
    )
    return {
        "eventCount": org.issue_event_count,
        "transactionEventCount": org.transaction_count,
        "uptimeCheckEventCount": org.uptime_check_event_count,
        "fileSizeMB": org.file_size,
    }
