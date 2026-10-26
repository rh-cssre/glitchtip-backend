from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("stripe", "0019_stripeprice_active_stripeproduct_active_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="stripeproduct",
            name="uptime_monitors",
            field=models.PositiveIntegerField(
                blank=True,
                help_text=(
                    "Maximum uptime monitors per organization (Stripe metadata "
                    "`uptime_monitors`). NULL means the plan expresses no opinion "
                    "and the built-in default applies."
                ),
                null=True,
            ),
        ),
    ]
