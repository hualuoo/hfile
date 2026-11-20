from django.db import migrations


class Migration(migrations.Migration):
    """What a new version's migration does: a new table with a row, and a change to a row that was there."""

    dependencies = [("siteconf", "0001_initial")]

    operations = [
        migrations.RunSQL(
            [
                "CREATE TABLE siteconf_upgrade_test (id integer PRIMARY KEY, note varchar(40))",
                "INSERT INTO siteconf_upgrade_test (id, note) VALUES (1, '升级测试')",
            ],
            reverse_sql=["DROP TABLE siteconf_upgrade_test"],
        ),
        migrations.RunSQL(
            "UPDATE siteconf_sitesettings SET site_name = '升级后的名字'",
            reverse_sql="UPDATE siteconf_sitesettings SET site_name = 'HFile'",
        ),
    ]
