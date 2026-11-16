from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("stripe", "0021_remove_stripesubscription_overage_counter"),
    ]

    operations = [
        migrations.AddField(
            model_name="stripesubscription",
            name="metered_period_start",
            field=models.DateTimeField(
                blank=True,
                help_text="Start of the metered item's current Stripe billing period.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="stripesubscription",
            name="metered_period_end",
            field=models.DateTimeField(
                blank=True,
                help_text="End of the metered item's current Stripe billing period.",
                null=True,
            ),
        ),
    ]
