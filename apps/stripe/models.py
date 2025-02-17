import logging

from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.organizations_ext.models import Organization

from .client import list_products, list_subscriptions
from .utils import unix_to_datetime

logger = logging.getLogger(__name__)


class StripeModel(models.Model):
    stripe_id = models.CharField(primary_key=True, max_length=28)

    class Meta:
        abstract = True


class StripeProduct(StripeModel):
    name = models.CharField()
    description = models.TextField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    events = models.PositiveBigIntegerField()
    is_public = models.BooleanField()

    def __str__(self):
        return f"{self.name} {self.stripe_id}"

    @classmethod
    async def sync_from_stripe(cls):
        stripe_ids = set()
        async for products in list_products():
            logger.info(f"Found {len(products)} products in Stripe")
            products = [
                StripeProduct(
                    stripe_id=product.id,
                    name=product.name,
                    description=product.description if product.description else "",
                    price=product.default_price.unit_amount / 100,
                    events=product.metadata["events"],
                    is_public=product.metadata.get("is_public") == "true",
                )
                for product in products
                if "events" in product.metadata
            ]
            updated = await StripeProduct.objects.abulk_create(
                products,
                update_conflicts=True,
                update_fields=["name", "description", "price", "events", "is_public"],
                unique_fields=["stripe_id"],
            )
            logger.info(f"Created/updated {len(updated)} products in Django")
            for obj in updated:
                stripe_ids.add(obj.stripe_id)
        result = await StripeProduct.objects.exclude(stripe_id__in=stripe_ids).adelete()
        if result[0]:
            logger.info(f"Deleted {result[0]} products in Django")


class StripeSubscription(StripeModel):
    is_active = models.BooleanField()
    is_primary = models.BooleanField(default=False)
    created = models.DateTimeField()
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    product = models.ForeignKey(StripeProduct, on_delete=models.CASCADE)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.SET_NULL, null=True
    )

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["organization", "is_primary"],
                condition=Q(is_primary=True),
                name="unique_primary_subscription_per_organization",
            ),
        ]

    @classmethod
    async def sync_from_stripe(cls):
        organization_ids = set()
        active_organization_ids = set()
        async for subscriptions in list_subscriptions():
            logger.info(f"Found {len(subscriptions)} subcriptions in Stripe")

            subscription_objects = []
            for subscription in subscriptions:
                org_metadata = subscription.customer.metadata
                try:
                    organization_id = int(
                        org_metadata.get(
                            "organization_id", org_metadata.get("djstripe_subscriber")
                        )
                    )
                except (ValueError, KeyError):
                    continue  # Skip if no organization ID in metadata

                items = subscription.items.data
                if not items or not items[0].get("price", {}).get("product"):
                    continue  # Skip

                product_id = items[0]["price"]["product"]

                # If unseen organization id, check if it exists
                if organization_id not in organization_ids:
                    organization_ids.add(organization_id)
                    organization = await Organization.objects.filter(
                        id=organization_id
                    ).afirst()
                    if organization:
                        active_organization_ids.add(organization_id)
                        if not organization.stripe_customer_id:
                            organization.stripe_customer_id = subscription.customer.id
                            await organization.asave(
                                update_fields=["stripe_customer_id"]
                            )
                # Only save subscriptions with organizations that exist
                if organization_id in active_organization_ids:
                    subscription_objects.append(
                        StripeSubscription(
                            stripe_id=subscription.id,
                            created=unix_to_datetime(subscription.created),
                            current_period_start=unix_to_datetime(
                                subscription.current_period_start
                            ),
                            current_period_end=unix_to_datetime(
                                subscription.current_period_end
                            ),
                            product_id=product_id,
                            organization_id=organization_id,
                            is_active=subscription.status == "active",
                        )
                    )

            stripe_subscriptions = await StripeSubscription.objects.abulk_create(
                subscription_objects,
                update_conflicts=True,
                update_fields=[
                    "created",
                    "current_period_start",
                    "current_period_end",
                    "product_id",
                    "organization_id",
                    "is_active",
                ],
                unique_fields=["stripe_id"],
            )
            logger.info(
                f"Created/updated {len(stripe_subscriptions)} subscriptions in Django"
            )
