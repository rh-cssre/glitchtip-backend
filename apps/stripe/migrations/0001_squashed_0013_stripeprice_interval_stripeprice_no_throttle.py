import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    replaces = [
        ("stripe", "0001_squashed_0006_stripeproduct_name"),
        ("stripe", "0007_remove_stripeproduct_price_alter_stripeproduct_name_and_more"),
        (
            "stripe",
            "0008_remove_stripesubscription_unique_primary_subscription_per_organization_and_more",
        ),
        ("stripe", "0009_remove_stripesubscription_product_and_more"),
        ("stripe", "0010_remove_stripesubscription_product_and_more"),
        ("stripe", "0011_remove_stripesubscription_is_active_and_more"),
        ("stripe", "0001_squashed_0011_remove_stripesubscription_is_active_and_more"),
        ("stripe", "0012_alter_stripesubscription_status"),
        ("stripe", "0013_stripeprice_interval_stripeprice_no_throttle"),
    ]

    initial = True

    dependencies = [
        ("organizations_ext", "0001_squashed_0008_merge_20250210_1625"),
    ]

    operations = [
        migrations.CreateModel(
            name="StripeProduct",
            fields=[
                (
                    "stripe_id",
                    models.CharField(max_length=30, primary_key=True, serialize=False),
                ),
                ("description", models.TextField()),
                ("events", models.PositiveBigIntegerField()),
                ("is_public", models.BooleanField()),
                ("name", models.CharField()),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="StripePrice",
            fields=[
                (
                    "stripe_id",
                    models.CharField(max_length=30, primary_key=True, serialize=False),
                ),
                ("price", models.DecimalField(decimal_places=2, max_digits=10)),
                ("nickname", models.CharField(max_length=255)),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="stripe.stripeproduct",
                    ),
                ),
                ("interval", models.CharField(default="month", max_length=20)),
                ("no_throttle", models.BooleanField(default=False)),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="stripeproduct",
            name="default_price",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="stripe.stripeprice",
            ),
        ),
        migrations.CreateModel(
            name="StripeSubscription",
            fields=[
                (
                    "stripe_id",
                    models.CharField(max_length=30, primary_key=True, serialize=False),
                ),
                ("current_period_start", models.DateTimeField()),
                ("current_period_end", models.DateTimeField()),
                (
                    "organization",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="organizations_ext.organization",
                    ),
                ),
                ("created", models.DateTimeField()),
                (
                    "price",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        to="stripe.stripeprice",
                    ),
                ),
                (
                    "collection_method",
                    models.CharField(
                        choices=[
                            ("charge_automatically", "Charge Automatically"),
                            ("send_invoice", "Send Invoice"),
                        ],
                        default="charge_automatically",
                        max_length=20,
                    ),
                ),
                ("start_date", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("incomplete", "Incomplete"),
                            ("incomplete_expired", "Incomplete Expired"),
                            ("trialing", "Trialing"),
                            ("active", "Active"),
                            ("past_due", "Past Due"),
                            ("canceled", "Canceled"),
                            ("unpaid", "Unpaid"),
                            ("paused", "Paused"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=18,
                    ),
                ),
                ("subscription_cycle_end", models.DateTimeField(blank=True, null=True)),
                (
                    "subscription_cycle_start",
                    models.DateTimeField(blank=True, null=True),
                ),
            ],
            options={
                "abstract": False,
                "constraints": [],
            },
        ),
    ]
