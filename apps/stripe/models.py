import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, models
from django.db.models.expressions import OuterRef, Subquery
from django.utils import timezone

from apps.organizations_ext.models import Organization

from .client import (
    cancel_subscription,
    fetch_subscription,
    list_prices,
    list_products,
    list_subscriptions,
)
from .constants import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    CollectionMethod,
    SubscriptionStatus,
)
from .exceptions import StripeResourceNotFound
from .utils import compute_cycle, unix_to_datetime

logger = logging.getLogger(__name__)


def _warn_duplicate_public_prices(prices):
    """Warn if any (product, interval) pair has more than one public price.

    The frontend's price-selection invariant assumes at most one
    is_public=True price per (product, interval). This check is per-page only;
    duplicates split across pages are caught by StripeProduct.sync_from_stripe,
    which loads default prices in a single pass per product.
    """
    grouped = defaultdict(list)
    for p in prices:
        if p.is_public:
            grouped[(p.product_id, p.interval)].append(p.stripe_id)
    for (product_id, interval), price_ids in grouped.items():
        if len(price_ids) > 1:
            logger.warning(
                "Multiple is_public=true prices for product=%s interval=%s: %s",
                product_id,
                interval,
                price_ids,
            )


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
    marketing_features = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"{self.name} {self.stripe_id}"

    @classmethod
    async def sync_from_stripe(cls):
        stripe_ids = set()
        async for products_page in list_products():
            logger.info(f"Found {len(products_page)} products in Stripe")
            products_page = [
                product
                for product in products_page
                if "events" in product.metadata
                and product.metadata.get("product_type", "").lower() == "hosted"
            ]
            products = [
                StripeProduct(
                    stripe_id=product.id,
                    name=product.name,
                    description=product.description if product.description else "",
                    events=product.metadata["events"],
                    is_public=product.metadata.get("is_public", "").lower() == "true",
                    marketing_features=[
                        f["name"] for f in product.marketing_features if f.get("name")
                    ],
                )
                for product in products_page
            ]
            prices = [
                StripePrice(
                    stripe_id=product.default_price.id,
                    price=product.default_price.unit_amount / 100,
                    nickname=product.default_price.nickname or "",
                    product_id=product.id,
                    no_throttle=product.default_price.metadata.get(
                        "no_throttle", ""
                    ).lower()
                    == "true"
                    if product.default_price.metadata
                    else False,
                    is_public=product.default_price.metadata.get(
                        "is_public", ""
                    ).lower()
                    == "true"
                    if product.default_price.metadata
                    else False,
                    interval=product.default_price.recurring.get("interval", "month")
                    if product.default_price.recurring
                    else "month",
                )
                for product in products_page
                if product.default_price
                and product.default_price.unit_amount is not None
            ]
            product_updated = await StripeProduct.objects.abulk_create(
                products,
                update_conflicts=True,
                update_fields=[
                    "name",
                    "description",
                    "events",
                    "is_public",
                    "marketing_features",
                ],
                unique_fields=["stripe_id"],
            )
            logger.info(f"Created/updated {len(product_updated)} products in Django")
            price_updated = await StripePrice.objects.abulk_create(
                prices,
                update_conflicts=True,
                update_fields=[
                    "price",
                    "nickname",
                    "product_id",
                    "no_throttle",
                    "is_public",
                    "interval",
                ],
                unique_fields=["stripe_id"],
            )
            logger.info(f"Created/updated {len(price_updated)} prices in Django")
            _warn_duplicate_public_prices(prices)
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

        # Keep products whose prices are still referenced by a subscription.
        # StripePrice.product cascades, but StripeSubscription.price is RESTRICT,
        # so deleting a product archived in Stripe while a (often grandfathered)
        # subscription still points at one of its prices raises RestrictedError
        # and aborts the whole sync. Retaining those products is harmless — they
        # simply stop appearing in Stripe's product list.
        referenced_product_ids = StripeSubscription.objects.values_list(
            "price__product_id", flat=True
        )
        result = await (
            StripeProduct.objects.exclude(stripe_id__in=stripe_ids)
            .exclude(stripe_id__in=referenced_product_ids)
            .adelete()
        )
        if result[0]:
            logger.info(f"Deleted {result[0]} products in Django")


class StripePrice(StripeModel):
    price = models.DecimalField(max_digits=10, decimal_places=2)
    nickname = models.CharField(max_length=255)
    product = models.ForeignKey(StripeProduct, on_delete=models.CASCADE)
    no_throttle = models.BooleanField(default=False)
    is_public = models.BooleanField(default=False)
    interval = models.CharField(max_length=20, default="month")

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
                    no_throttle=price.metadata.get("no_throttle", "").lower() == "true"
                    if price.metadata
                    else False,
                    is_public=price.metadata.get("is_public", "").lower() == "true"
                    if price.metadata
                    else False,
                    interval=price.recurring.get("interval", "month")
                    if price.recurring
                    else "month",
                )
                for price in prices_page
                if price.unit_amount is not None and price.product in known_product_ids
            ]
            await StripePrice.objects.abulk_create(
                prices,
                update_conflicts=True,
                update_fields=[
                    "price",
                    "nickname",
                    "product_id",
                    "no_throttle",
                    "is_public",
                    "interval",
                ],
                unique_fields=["stripe_id"],
            )
            _warn_duplicate_public_prices(prices)


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
    subscription_cycle_start = models.DateTimeField(null=True, blank=True)
    subscription_cycle_end = models.DateTimeField(null=True, blank=True)

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
    async def cancel_for_organization(cls, organization: Organization):
        async for subscription in cls.objects.filter(
            organization=organization, status__in=ACTIVE_SUBSCRIPTION_STATUSES
        ):
            try:
                await cancel_subscription(subscription.stripe_id)
            except StripeResourceNotFound:
                logger.info(
                    "Subscription %s already absent from Stripe; skipping cancel",
                    subscription.stripe_id,
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
        ).select_related("price"):
            try:
                fetched_sub = await fetch_subscription(subscription.stripe_id)
            except StripeResourceNotFound:
                logger.error(
                    f"Stripe did not return subscription for {subscription.stripe_id}"
                )
                continue
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

            is_annual = subscription.price.interval == "year" or bool(
                fetched_sub.items.data[0].price.recurring
                and fetched_sub.items.data[0].price.recurring.get("interval") == "year"
            )
            (
                subscription.subscription_cycle_start,
                subscription.subscription_cycle_end,
            ) = compute_cycle(
                subscription.current_period_start,
                subscription.current_period_end,
                is_annual,
            )
            await subscription.asave(
                update_fields=[
                    "status",
                    "created",
                    "current_period_start",
                    "current_period_end",
                    "start_date",
                    "collection_method",
                    "subscription_cycle_start",
                    "subscription_cycle_end",
                ]
            )

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
                    # Update price (just in case we're out of date)
                    if price_id not in known_price_ids:
                        try:
                            await StripePrice.objects.aupdate_or_create(
                                stripe_id=price_id,
                                defaults={
                                    "product_id": price.product,
                                    "nickname": price.nickname or "",
                                    "price": price.unit_amount / 100,
                                },
                            )
                            known_price_ids.add(price_id)
                        except IntegrityError:
                            # Should not happen, notify, move on
                            # Could happen if a customer has GT subscription and
                            # other unrelated stripe subscription.
                            logger.warning(
                                f"Failed to create StripePrice {price_id}",
                                exc_info=True,
                            )
                            continue

                    period_start = unix_to_datetime(
                        subscription.items.data[0].current_period_start
                    )
                    period_end = unix_to_datetime(
                        subscription.items.data[0].current_period_end
                    )
                    is_annual = bool(
                        price.recurring and price.recurring.get("interval") == "year"
                    )
                    cycle_start, cycle_end = compute_cycle(
                        period_start, period_end, is_annual
                    )

                    subscription_objects.append(
                        StripeSubscription(
                            stripe_id=subscription.id,
                            created=unix_to_datetime(subscription.created),
                            current_period_start=period_start,
                            current_period_end=period_end,
                            price_id=price_id,
                            organization_id=organization_id,
                            status=subscription.status,
                            start_date=unix_to_datetime(subscription.start_date),
                            collection_method=subscription.collection_method,
                            subscription_cycle_start=cycle_start,
                            subscription_cycle_end=cycle_end,
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
                    "subscription_cycle_start",
                    "subscription_cycle_end",
                ],
                unique_fields=["stripe_id"],
            )
            logger.info(
                f"Created/updated {len(stripe_subscriptions)} subscriptions in Django"
            )

        await cls.set_primary_subscriptions_for_organizations(active_organization_ids)
        await cls.update_outdated_subscriptions()
        await cls.remove_inactive_primary_subscriptions()


class SupportLicense(models.Model):
    """Singleton (pk=1): one instance-wide support-plan license per server."""

    license_key = models.CharField(max_length=255, blank=True, default="")
    billing_email = models.EmailField(blank=True, default="")
    updated = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    async def load(cls):
        obj, _ = await cls.objects.aget_or_create(pk=1)
        return obj

    @classmethod
    async def resolved(cls) -> tuple[str, str]:
        if settings.BILLING_ENABLED:
            return ("", "")
        # Env var wins and short-circuits the DB query when set. Fall back to
        # the DB row otherwise. billing_email is DB-only — env carries no email.
        if settings.GLITCHTIP_LICENSE_KEY:
            return (settings.GLITCHTIP_LICENSE_KEY, "")
        db = await cls.load()
        return (db.license_key, db.billing_email)
