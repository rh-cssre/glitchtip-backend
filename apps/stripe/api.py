from ninja import ModelSchema, Router

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.schema import CamelSchema

from .models import StripeProduct, StripeSubscription

router = Router()


class StripeProductSchema(CamelSchema, ModelSchema):
    class Meta:
        model = StripeProduct
        fields = ["stripe_id", "name", "description", "price", "events"]


class StripeSubscriptionSchema(CamelSchema, ModelSchema):
    class Meta:
        model = StripeSubscription
        fields = ["stripe_id", "created", "current_period_start", "current_period_end"]


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
        .select_related("product")
        .order_by("-created")
        .afirst()
    )
