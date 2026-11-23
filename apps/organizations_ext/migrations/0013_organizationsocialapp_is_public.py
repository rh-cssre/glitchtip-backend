from django.db import migrations, models

HELP_TEXT = (
    "Show this sign-in option on the instance-wide login page, alongside the "
    "instance's own providers. When false it appears only on the "
    "organization's own login page. Settable here only — the organization API "
    "never writes it."
)


class Migration(migrations.Migration):
    """
    Add is_public, defaulting existing rows to True.

    Before this field existed the instance-wide login page listed every social
    app, including organization-linked ones, so True is what those installs
    already do — backfilling False would make working sign-in buttons vanish on
    upgrade. New rows default to False, hence the AlterField.
    """

    dependencies = [
        ("organizations_ext", "0012_organization_metered_billing"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationsocialapp",
            name="is_public",
            field=models.BooleanField(default=True, help_text=HELP_TEXT),
        ),
        migrations.AlterField(
            model_name="organizationsocialapp",
            name="is_public",
            field=models.BooleanField(default=False, help_text=HELP_TEXT),
        ),
    ]
