# Generated by Django 3.0.7 on 2020-06-13 21:47

import logging
from django.db import migrations, models
from django.db.utils import IntegrityError

logger = logging.getLogger(__name__)


def move_members(apps, schema_editor):
    Team = apps.get_model("teams", "Team")
    for team in Team.objects.all():
        for member in team.members.all():
            org_user = team.organization.organization_users.filter(user=member).first()
            try:
                team.members_new.add(org_user)
            except IntegrityError:
                logger.warning("Unable to add team member %s", org_user)


class Migration(migrations.Migration):

    dependencies = [
        ("organizations_ext", "0001_squashed_0009_organization_scrub_ip_addresses"),
        ("teams", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="team",
            name="members_new",
            field=models.ManyToManyField(to="organizations_ext.OrganizationUser"),
        ),
        migrations.RunPython(move_members),
    ]
