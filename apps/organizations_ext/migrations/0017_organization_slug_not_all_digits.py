import django.core.validators
from django.db import migrations, models

import apps.organizations_ext.fields

ALL_DIGITS_SLUG = r"^[0-9]+$"
SLUG_MAX_LENGTH = 200


def rename_all_digit_slugs(apps, schema_editor):
    """Give every all-digit slug a ``-N`` suffix so the constraint can land.

    Deleted organizations are renamed too: the constraint covers every row.
    The first free suffix is taken, so ``2024`` becomes ``2024-1`` unless that
    slug is already in use.
    """
    Organization = apps.get_model("organizations_ext", "Organization")
    renamed = []
    for org in (
        Organization.objects.filter(slug__regex=ALL_DIGITS_SLUG)
        .order_by("id")
        .only("id", "slug")
    ):
        suffix = 1
        while True:
            tail = f"-{suffix}"
            slug = org.slug[: SLUG_MAX_LENGTH - len(tail)] + tail
            if not Organization.objects.filter(slug=slug).exists():
                break
            suffix += 1
        Organization.objects.filter(id=org.id).update(slug=slug)
        renamed.append((org.id, org.slug, slug))

    if renamed:
        print(
            f"\n  Renamed {len(renamed)} organization slug(s) that were only "
            "digits, which are now reserved for organization ids:"
        )
        for org_id, old, new in renamed:
            print(f"    organization {org_id}: {old} -> {new}")


class Migration(migrations.Migration):
    dependencies = [
        ("organizations_ext", "0016_alter_organization_throttle_help_text"),
    ]

    operations = [
        migrations.RunPython(rename_all_digit_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="organization",
            name="slug",
            field=apps.organizations_ext.fields.OrganizationSlugField(
                blank=True,
                editable=False,
                help_text="The name in all lowercase, suitable for URL identification",
                max_length=SLUG_MAX_LENGTH,
                populate_from="name",
                unique=True,
                validators=[
                    django.core.validators.RegexValidator(
                        ALL_DIGITS_SLUG,
                        inverse_match=True,
                        message="A slug cannot be only digits.",
                    )
                ],
            ),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=~models.Q(slug__regex=ALL_DIGITS_SLUG),
                name="organization_slug_not_all_digits",
            ),
        ),
    ]
