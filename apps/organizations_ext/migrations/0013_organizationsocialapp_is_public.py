from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations_ext", "0012_organization_metered_billing"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationsocialapp",
            name="is_public",
            field=models.BooleanField(
                default=True,
                help_text="Show this sign-in option on the instance-wide login "
                "page. When false it only appears on the organization's own "
                "login page (/login/<org slug>).",
            ),
        ),
    ]
