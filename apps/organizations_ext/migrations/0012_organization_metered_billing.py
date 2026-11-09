from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations_ext", "0011_organization_is_deleted"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="metered_billing_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Opt-in: charge for billable events above plan quota via "
                "Stripe metered billing, up to overage_spend_cap_cents.",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="overage_spend_cap_cents",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Max overage spend per billing cycle, in cents. Metered "
                "billing stops reporting (and throttling resumes) once reached.",
            ),
        ),
    ]
