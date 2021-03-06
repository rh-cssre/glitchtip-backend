# Generated by Django 3.2.7 on 2021-09-29 15:06

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('organizations_ext', '0001_squashed_0009_organization_scrub_ip_addresses'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrganizationInvitation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('guid', models.UUIDField(editable=False)),
                ('invitee_identifier', models.CharField(help_text='The contact identifier for the invitee, email, phone number, social media handle, etc.', max_length=1000)),
                ('invited_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='organizations_ext_organizationinvitation_sent_invitations', to=settings.AUTH_USER_MODEL)),
                ('invitee', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='organizations_ext_organizationinvitation_invitations', to=settings.AUTH_USER_MODEL)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='organization_invites', to='organizations_ext.organization')),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
