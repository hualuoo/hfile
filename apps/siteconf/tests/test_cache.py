"""SiteSettings.load()'s cache: kept between requests, read again after a save. Outside transactions
only, so these tests are TransactionTestCases (a TestCase runs inside one: load() reads the database)."""

from django.core.cache import cache
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from .. import models
from ..models import SiteSettings


class CacheTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        SiteSettings.objects.all().delete()

    def test_kept_and_read_again_after_a_save(self):
        SiteSettings.load()  # makes the row: that is a save, so the next load reads it again
        first = SiteSettings.load()
        with self.assertNumQueries(0):
            again = SiteSettings.load()
        self.assertEqual(again.pk, first.pk)
        self.assertIsNot(again, first)  # a copy each: changing one changes nobody else's
        again.site_name = "改了没存"
        self.assertEqual(SiteSettings.load().site_name, "HFile")

        first.site_name = "新名字"
        first.save()
        with self.assertNumQueries(1):
            self.assertEqual(SiteSettings.load().site_name, "新名字")
        with self.assertNumQueries(0):
            SiteSettings.load()

    def test_a_change_it_didnt_see(self):
        SiteSettings.load()
        SiteSettings.load()
        SiteSettings.objects.update(site_name="直接改的")  # no save(): the copy stays
        self.assertEqual(SiteSettings.load().site_name, "HFile")
        models.forget_cached()
        self.assertEqual(SiteSettings.load().site_name, "直接改的")

    def test_a_copy_read_while_it_was_saved_isnt_kept(self):
        # Another thread read the row, then this one saved it, then the other put its (old) copy in
        # the cache: its generation is older, so it is never used.
        SiteSettings.load()
        generation = models._generation()
        old = SiteSettings.load()
        fresh = SiteSettings.objects.get(pk=1)
        fresh.site_name = "保存后"
        fresh.save()
        cache.set(models._ROW_KEY, (generation, old), models.CACHE_SECONDS)
        self.assertEqual(SiteSettings.load().site_name, "保存后")

    def test_not_inside_transactions(self):
        SiteSettings.load()
        with transaction.atomic():
            site = SiteSettings.objects.select_for_update().get(pk=1)
            site.site_name = "回滚的名字"
            site.save()
            self.assertEqual(SiteSettings.load().site_name, "回滚的名字")  # this transaction's own
            transaction.set_rollback(True)
        self.assertEqual(SiteSettings.load().site_name, "HFile")  # what was rolled back isn't kept

    def test_after_a_migration_and_when_the_cache_is_emptied(self):
        SiteSettings.load()
        SiteSettings.load()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE siteconf_sitesettings SET site_name = %s", ["迁移改的"])
        self.assertEqual(SiteSettings.load().site_name, "HFile")  # not seen yet
        from django.apps import apps
        from django.db.models.signals import post_migrate

        config = apps.get_app_config("siteconf")
        post_migrate.send(sender=config, app_config=config, verbosity=0, interactive=False, using="default", plan=[], apps=apps)
        self.assertEqual(SiteSettings.load().site_name, "迁移改的")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE siteconf_sitesettings SET site_name = %s", ["清空缓存后"])
        cache.clear()  # the generation goes too: a new one, which no copy has
        self.assertEqual(SiteSettings.load().site_name, "清空缓存后")

    def test_the_row_is_made_when_missing(self):
        self.assertEqual(SiteSettings.objects.count(), 0)
        self.assertEqual(SiteSettings.load().site_name, "HFile")
        SiteSettings.load()
        self.assertEqual(SiteSettings.objects.count(), 1)


class InsideTestCaseTests(TestCase):
    def test_reads_the_database_in_a_transaction(self):
        SiteSettings.load()
        SiteSettings.objects.update(site_name="直接改的")
        self.assertEqual(SiteSettings.load().site_name, "直接改的")
