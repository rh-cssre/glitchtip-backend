# Generated by Django 3.2 on 2021-04-16 18:58

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('organizations_ext', '0001_squashed_0009_organization_scrub_ip_addresses'),
        ('files', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='fileblob',
            name='organizations',
            field=models.ManyToManyField(to='organizations_ext.Organization'),
        ),
    ]