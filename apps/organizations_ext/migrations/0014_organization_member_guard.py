from django.db import migrations

from apps.shared.migration_utils import get_sql_content


class Migration(migrations.Migration):
    dependencies = [
        ("organizations_ext", "0013_organizationsocialapp_is_public"),
    ]

    operations = [
        migrations.RunSQL(
            sql=get_sql_content(__file__, "organization_member_guard.sql"),
            reverse_sql=(
                "DROP TRIGGER IF EXISTS organization_has_members"
                " ON organizations_ext_organizationuser;\n"
                "DROP FUNCTION IF EXISTS organizations_ext_org_member_guard();"
            ),
        ),
    ]
