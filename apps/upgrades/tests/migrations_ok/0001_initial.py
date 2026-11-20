# siteconf's real first migration, so that the test database (migrated with it) has it applied.
import importlib

Migration = importlib.import_module("apps.siteconf.migrations.0001_initial").Migration
