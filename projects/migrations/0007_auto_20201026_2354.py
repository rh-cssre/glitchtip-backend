# Generated by Django 3.1.2 on 2020-10-26 23:54

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0006_project_scrub_ip_addresses'),
    ]

    operations = [
        migrations.AlterField(
            model_name='project',
            name='scrub_ip_addresses',
            field=models.BooleanField(default=True, help_text='Should project anonymize IP Addresses'),
        ),
    ]
