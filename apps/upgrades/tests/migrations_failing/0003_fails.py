from django.db import migrations


def fail(apps, schema_editor):
    raise RuntimeError("这个迁移故意失败（升级的测试）")


class Migration(migrations.Migration):
    dependencies = [("siteconf", "0002_upgrade_test")]

    operations = [
        migrations.RunSQL("CREATE TABLE siteconf_half_done (id integer)", reverse_sql="DROP TABLE siteconf_half_done"),
        migrations.RunSQL("UPDATE siteconf_upgrade_test SET note = 'broken'", reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL("UPDATE accounts_user SET nickname = 'broken'", reverse_sql=migrations.RunSQL.noop),
        migrations.RunPython(fail, migrations.RunPython.noop),
    ]
