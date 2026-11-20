"""Test migrations for siteconf whose last one fails half way (apps/upgrades/tests): 0002 works, 0003 changes
tables and data and then raises. MySQL can't take back what 0003 did before it failed (DDL commits at
once), and SQLite only takes back 0003, not 0002: the upgrade has to put the backup back."""
