import logging
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models.expressions import OuterRef, Subquery
from django.utils import timezone

from apps.organizations_ext.models import Organization

from .client import fetch_subscription, list_prices, list_products, list_subscriptions
from .constants import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    CollectionMethod,
    SubscriptionStatus,
)
from .utils import unix_to_datetime

logger = logging.getLogger(__name__)


class StripeModel(models.Model):
    stripe_id = models.CharField(primary_key=True, max_length=30)

    class Meta:
        abstract = True


class StripeProduct(StripeModel):
    name = models.CharField()
    description = models.TextField()
    default_price = models.ForeignKey(
        "StripePrice", on_delete=models.CASCADE, blank=True, null=True
    )
    events = models.PositiveBigIntegerField()
    is_public = models.BooleanField()

    def __str__(self):
        return f"{self.name} {self.stripe_id}"

    @classmethod
    async def sync_from_stripe(cls):
        stripe_ids = set()
        async for products_page in list_products():
            logger.info(f"Found {len(products_page)} products in Stripe")
            products_page = [
                product for product in products_page if "events" in product.metadata
            ]
            products = [
                StripeProduct(
                    stripe_id=product.id,
                    name=product.name,
                    description=product.description if product.description else "",
                    events=product.metadata["events"],
                    is_public=product.metadata.get("is_public") == "true",
                )
                for product in products_page
            ]
            prices = [
                StripePrice(
                    stripe_id=product.default_price.id,
                    price=product.default_price.unit_amount / 100,
                    nickname=product.default_price.nickname or "",
                    product_id=product.id,
                )
                for product in products_page
                if product.default_price
                and product.default_price.unit_amount is not None
            ]
            product_updated = await StripeProduct.objects.abulk_create(
                products,
                update_conflicts=True,
                update_fields=["name", "description", "events", "is_public"],
                unique_fields=["stripe_id"],
            )
            logger.info(f"Created/updated {len(product_updated)} products in Django")
            price_updated = await StripePrice.objects.abulk_create(
                prices,
                update_conflicts=True,
                update_fields=["price", "nickname", "product_id"],
                unique_fields=["stripe_id"],
            )
            logger.info(f"Created/updated {len(price_updated)} prices in Django")
            for product in product_updated:
                for price in price_updated:
                    if (
                        price.product_id == product.stripe_id
                        and product.default_price_id != price.stripe_id
                    ):
                        product.default_price_id = price.stripe_id
                        await product.asave(update_fields=["default_price_id"])

            for obj in product_updated:
                stripe_ids.add(obj.stripe_id)

        result = await StripeProduct.objects.exclude(stripe_id__in=stripe_ids).adelete()
        if result[0]:
            logger.info(f"Deleted {result[0]} products in Django")


class StripePrice(StripeModel):
    price = models.DecimalField(max_digits=10, decimal_places=2)
    nickname = models.CharField(max_length=255)
    product = models.ForeignKey(StripeProduct, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.nickname} {self.price} {self.stripe_id}"

    @classmethod
    async def sync_from_stripe(cls):
        async for prices_page in list_prices():
            product_ids = {price.product for price in prices_page}
            products = StripeProduct.objects.filter(stripe_id__in=product_ids)
            known_product_ids = set()
            async for product in products:
                known_product_ids.add(product.stripe_id)

            prices = [
                StripePrice(
                    stripe_id=price.id,
                    price=price.unit_amount / 100,
                    nickname=price.nickname or "",
                    product_id=price.product,
                )
                for price in prices_page
                if price.unit_amount is not None and price.product in known_product_ids
            ]
            await StripePrice.objects.abulk_create(
                prices,
                update_conflicts=True,
                update_fields=["price", "nickname", "product_id"],
                unique_fields=["stripe_id"],
            )


class StripeSubscription(StripeModel):
    created = models.DateTimeField()
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    price = models.ForeignKey(StripePrice, on_delete=models.RESTRICT)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.SET_NULL, null=True
    )
    status = models.CharField(
        max_length=18,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
        db_index=True,
    )
    collection_method = models.CharField(
        max_length=20,
        choices=CollectionMethod.choices,
        default=CollectionMethod.CHARGE_AUTOMATICALLY,
    )
    start_date = models.DateTimeField()

    def __str__(self):
        return f"{self.stripe_id}"

    @classmethod
    async def get_primary_subscription(cls, organization: Organization):
        return (
            await cls.objects.filter(
                organization=organization, status__in=ACTIVE_SUBSCRIPTION_STATUSES
            )
            .order_by("-price__product__events", "-created")
            .afirst()
        )

    @classmethod
    async def set_primary_subscriptions_for_organizations(
        cls, organization_ids: set[int]
    ):
        # This subquery finds the primary subscription ID for each organization.
        primary_subscription_subquery = (
            cls.objects.filter(
                organization_id=OuterRef("pk"), status__in=ACTIVE_SUBSCRIPTION_STATUSES
            )
            .order_by("-price__product__events", "-created")
            .values("pk")[:1]
        )

        org_updates = []
        async for org in Organization.objects.filter(id__in=organization_ids).annotate(
            primary_subscription_id=Subquery(primary_subscription_subquery)
        ):
            if org.primary_subscription_id != org.stripe_primary_subscription_id:
                org.stripe_primary_subscription_id = org.primary_subscription_id
                org_updates.append(org)

        if org_updates:
            await Organization.objects.abulk_update(
                org_updates, ["stripe_primary_subscription"]
            )

    @classmethod
    async def update_outdated_subscriptions(cls):
        async for subscription in cls.objects.filter(
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            current_period_end__lt=(timezone.now() - timedelta(days=2)),
        ):
            fetched_sub = await fetch_subscription(subscription.stripe_id)
            subscription.status = fetched_sub.status
            subscription.created = unix_to_datetime(fetched_sub.created)
            subscription.current_period_start = unix_to_datetime(
                fetched_sub.items.data[0].current_period_start
            )
            subscription.current_period_end = unix_to_datetime(
                fetched_sub.items.data[0].current_period_end
            )
            subscription.start_date = unix_to_datetime(fetched_sub.start_date)
            subscription.collection_method = fetched_sub.collection_method
            await subscription.asave()

    @classmethod
    async def remove_inactive_primary_subscriptions(cls):
        await (
            Organization.objects.filter(stripe_primary_subscription__isnull=False)
            .exclude(
                stripe_primary_subscription__status__in=ACTIVE_SUBSCRIPTION_STATUSES
            )
            .aupdate(stripe_primary_subscription=None)
        )

    @classmethod
    async def sync_from_stripe(cls):
        organization_ids = set()
        active_organization_ids = set()
        known_price_ids = set()
        async for subscriptions in list_subscriptions():
            logger.info(f"Found {len(subscriptions)} subcriptions in Stripe")

            subscription_objects = []
            for subscription in subscriptions:
                org_metadata = subscription.customer.metadata
                if (
                    org_metadata is None
                    or org_metadata.get("region", "") != settings.STRIPE_REGION
                ):
                    continue  # Skip orgs in other regions
                try:
                    organization_id = int(
                        org_metadata.get(
                            "organization_id", org_metadata.get("djstripe_subscriber")
                        )
                    )
                except (ValueError, TypeError):
                    continue  # Skip if no organization ID in metadata

                items = subscription.items.data
                if not items or not items[0].price:
                    continue  # Skip

                price = items[0].price
                price_id = price.id
                if price_id not in known_price_ids:
                    await StripePrice.objects.aupdate_or_create(
                        stripe_id=price_id,
                        defaults={
                            "product_id": price.product,
                            "nickname": price.nickname or "",
                            "price": price.unit_amount / 100,
                        },
                    )
                    known_price_ids.add(price_id)

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
                                subscription.items.data[0].current_period_start
                            ),
                            current_period_end=unix_to_datetime(
                                subscription.items.data[0].current_period_end
                            ),
                            price_id=price_id,
                            organization_id=organization_id,
                            status=subscription.status,
                            start_date=unix_to_datetime(subscription.start_date),
                            collection_method=subscription.collection_method,
                        )
                    )

            stripe_subscriptions = await StripeSubscription.objects.abulk_create(
                subscription_objects,
                update_conflicts=True,
                update_fields=[
                    "created",
                    "current_period_start",
                    "current_period_end",
                    "price_id",
                    "organization_id",
                    "status",
                    "start_date",
                    "collection_method",
                ],
                unique_fields=["stripe_id"],
            )
            logger.info(
                f"Created/updated {len(stripe_subscriptions)} subscriptions in Django"
            )

        await cls.set_primary_subscriptions_for_organizations(active_organization_ids)
        await cls.update_outdated_subscriptions()
        await cls.remove_inactive_primary_subscriptions()
