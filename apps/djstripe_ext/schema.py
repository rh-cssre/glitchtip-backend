from djstripe.models import Price, Product, Subscription, SubscriptionItem
from ninja import ModelSchema

from glitchtip.schema import CamelSchema


class PriceIDSchema(CamelSchema):
    price: str


class SubscriptionIn(PriceIDSchema):
    organization: int


class ProductSchema(CamelSchema, ModelSchema):
    class Meta:
        model = Product
        fields = ["id", "name", "description", "type", "metadata"]


class PriceSchema(CamelSchema, ModelSchema):
    product: ProductSchema

    class Meta:
        model = Price
        fields = [
            "id",
            "nickname",
            "currency",
            "unit_amount",
            # "human_readable_price",
            "metadata",
            "product",
        ]


class SubscriptionItemSchema(CamelSchema, ModelSchema):
    price: PriceSchema

    class Meta:
        model = SubscriptionItem
        fields = ["id", "price"]


class SubscriptionSchema(CamelSchema, ModelSchema):
    items: list[SubscriptionItemSchema]

    class Meta:
        model = Subscription
        exclude = ["default_tax_rates"]


class CreateSubscriptionResponse(SubscriptionIn):
    subscription: SubscriptionSchema


class ProductPriceSchema(CamelSchema, ModelSchema):
    prices: list[PriceSchema]

    class Meta:
        model = Product
        fields = ["id", "name", "description", "type", "metadata"]
