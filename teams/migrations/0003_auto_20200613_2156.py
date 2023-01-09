# Generated by Django 3.0.7 on 2020-06-13 21:56

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations_ext", "0001_squashed_0009_organization_scrub_ip_addresses"),
        ("teams", "0002_team_members_new"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="team",
            name="members",
        ),
        migrations.RenameField(
            model_name="team",
            old_name="members_new",
            new_name="members",
        ),
    ]
