"""Driver instances (apps/storages/instances.py): one live instance per storage, a driver's own changes
saved without a rebuild, replaced after a save, none for a disabled storage, temporary ones that save
only what they must."""

import threading
from unittest import mock

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from apps.storages import config, instances
from apps.storages.models import StorageSource

from .fakes import FakeDriver, FakeTypeMixin


def make_storage(key="fake", **config_values):
    values = {"endpoint": "https://fake.example", "refresh_token": "refresh-0", "token": "access-0", **config_values}
    return StorageSource.objects.create(key=key, name=key, type="fake", config=config.encode(FakeDriver, values))


def stored(storage, key):
    storage.refresh_from_db()
    plain, _ = config.decode(FakeDriver, storage.config)
    return plain.get(key)


class LiveInstanceTests(FakeTypeMixin, TestCase):
    def test_one_instance_made_lazily_and_shared(self):
        storage = make_storage()
        self.assertEqual(FakeDriver.instances, [])
        first = instances.get(storage)
        self.assertIs(instances.get(storage), first)
        self.assertIs(instances.get(StorageSource.objects.get(pk=storage.pk)), first)
        self.assertEqual(len(FakeDriver.instances), 1)
        self.assertEqual(first.config["refresh_token"], "refresh-0")  # secrets decrypted for the driver

    def test_made_once_by_threads_at_the_same_time(self):
        storage = make_storage()
        got = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            got.append(instances.get(storage))

        # Threads have their own database connections, which don't see this test's transaction: the
        # instance is made by this thread first, the others find it.
        instances.get(storage)
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({id(driver) for driver in got}), 1)
        self.assertEqual(len(FakeDriver.instances), 1)

    def test_disabled_storages_have_none(self):
        storage = make_storage()
        storage.enabled = False
        storage.save()
        with self.assertRaises(instances.StorageUnavailable):
            instances.get(storage)
        self.assertEqual(FakeDriver.instances, [])

    def test_a_refreshed_token_is_saved_without_a_rebuild(self):
        storage = make_storage()
        driver = instances.get(storage)
        driver.refresh()
        self.assertEqual(stored(storage, "refresh_token"), "refresh-1")
        self.assertEqual(stored(storage, "token"), "access-1")
        self.assertEqual(stored(storage, "expires_at"), 1001)  # a secret number
        self.assertTrue(storage.config["refresh_token"].startswith("enc:"))
        self.assertIsNotNone(storage.tokens_saved_at)
        self.assertEqual(storage.config_revision, 1)  # no rebuild
        self.assertIs(instances.get(storage), driver)
        driver.refresh()
        self.assertEqual(stored(storage, "refresh_token"), "refresh-2")

    def test_a_new_revision_replaces_and_closes_the_old_one(self):
        storage = make_storage()
        old = instances.get(storage)
        StorageSource.objects.filter(pk=storage.pk).update(config_revision=2)
        storage.refresh_from_db()
        new = instances.get(storage)
        self.assertIsNot(new, old)
        self.assertTrue(old.closed)  # GRACE is 0 in the tests
        # A stale row (revision 1) gets the newer instance, not a rebuild back.
        StorageSource.objects.filter(pk=storage.pk)
        stale = StorageSource.objects.get(pk=storage.pk)
        stale.config_revision = 1
        self.assertIs(instances.get(stale), new)

    def test_an_old_instance_does_not_overwrite_what_the_admin_changed(self):
        storage = make_storage()
        old = instances.get(storage)
        # The admin authorises again: a new refresh token, a new revision.
        values, _ = config.decode(FakeDriver, storage.config)
        values["refresh_token"] = "from-the-admin"
        StorageSource.objects.filter(pk=storage.pk).update(config=config.encode(FakeDriver, values), config_revision=2)
        old.refresh()  # a request still running on the old instance refreshes with the old token
        self.assertEqual(stored(storage, "refresh_token"), "from-the-admin")
        self.assertEqual(stored(storage, "token"), "access-1")  # this one the admin kept: saved

    def test_an_old_instances_rotation_reaches_the_new_one(self):
        storage = make_storage()
        old = instances.get(storage)
        StorageSource.objects.filter(pk=storage.pk).update(config_revision=2)  # qps changed, tokens kept
        storage.refresh_from_db()
        new = instances.get(storage)
        old.refresh()  # 115: the old refresh token is dead now
        self.assertEqual(stored(storage, "refresh_token"), "refresh-1")
        self.assertEqual(new.config["refresh_token"], "refresh-1")  # passed on to the live instance

    def test_changes_inside_a_transaction_wait_for_it(self):
        storage = make_storage()
        driver = instances.get(storage)
        with transaction.atomic():
            driver.refresh()
            self.assertEqual(stored(storage, "refresh_token"), "refresh-0")
        # The test's own transaction is still open, so on_commit doesn't run: the next use saves it.
        handle = instances.live(storage.pk)
        self.assertEqual(handle.unsaved["refresh_token"], "refresh-1")
        instances.get(storage)
        self.assertEqual(stored(storage, "refresh_token"), "refresh-1")
        self.assertEqual(handle.unsaved, {})

    def test_a_change_from_a_rolled_back_transaction_never_undoes_a_later_one(self):
        storage = make_storage()
        driver = instances.get(storage)
        with transaction.atomic():
            driver.refresh()  # refresh-1 waits for the transaction, which then rolls back
            transaction.set_rollback(True)
        driver.refresh()  # refresh-2, saved at once
        self.assertEqual(stored(storage, "refresh_token"), "refresh-2")
        instances.get(storage)  # the next use saves what waited: not refresh-1 any more
        self.assertEqual(stored(storage, "refresh_token"), "refresh-2")
        self.assertEqual(driver.config["refresh_token"], "refresh-2")

    def test_drop_waits_for_an_instance_being_built(self):
        storage = make_storage()
        instances.get(storage)
        lock = instances._build_lock(storage.pk)
        lock.acquire()  # as get() holds it while it builds and installs an instance
        try:
            dropper = threading.Thread(target=instances.drop, args=(storage.pk,))
            dropper.start()
            dropper.join(0.2)
            self.assertTrue(dropper.is_alive())
        finally:
            lock.release()
        dropper.join(5)
        self.assertIsNone(instances.live(storage.pk))

    def test_no_locks_are_kept(self):
        """OPS-08: a storage's lock for building its instance and its lock for saving the driver's changes
        stayed in memory for good, a deleted storage's too. They are there only while in use."""
        storage = make_storage()
        instances.get(storage).refresh()  # built under the one lock, the new tokens saved under the other
        self.assertEqual(stored(storage, "refresh_token"), "refresh-1")
        self.assertNotIn(storage.pk, instances._building)
        self.assertNotIn(storage.pk, instances._save_locks)

    def test_a_driver_that_cannot_start(self):
        storage = make_storage()
        original = FakeDriver.__init__

        def broken(driver, config, *, on_update=None):
            raise RuntimeError("a bug")

        FakeDriver.__init__ = broken
        try:
            with self.assertLogs("hfile.storages", "ERROR"):
                with self.assertRaises(instances.StorageUnavailable) as caught:
                    instances.get(storage)
        finally:
            FakeDriver.__init__ = original
        self.assertIn("请管理员检查", caught.exception.message)
        self.assertFalse(instances.status(storage.pk)["ok"])
        self.assertIsNotNone(instances.get(storage))  # fixed: made on the next use

    def test_deleting_the_row_drops_the_instance(self):
        storage = make_storage()
        driver = instances.get(storage)
        with self.captureOnCommitCallbacks(execute=True):
            StorageSource.objects.filter(pk=storage.pk).delete()
        self.assertIsNone(instances.live(storage.pk))
        self.assertTrue(driver.closed)

    def test_unreadable_secrets_are_reported(self):
        storage = make_storage()
        from django.test import override_settings

        with override_settings(SECRET_KEY="another-key-" + "y" * 40):
            driver = instances.get(storage)
        self.assertNotIn("refresh_token", driver.config)
        self.assertIn("secret_key 换过", instances.status(storage.pk)["message"])

    def test_capabilities_from_the_instance(self):
        storage = make_storage()
        caps = instances.capabilities(storage)
        self.assertEqual(caps["range"], True)
        instances.get(storage).can_range = False  # FTP sets it from its params
        self.assertFalse(instances.capabilities(storage)["range"])
        storage.enabled = False
        self.assertTrue(instances.capabilities(storage)["range"])  # disabled: the class's


class TemporaryInstanceTests(FakeTypeMixin, TestCase):
    def test_a_spent_stored_token_is_saved(self):
        storage = make_storage()
        live = instances.get(storage)
        plain, _ = config.decode(FakeDriver, storage.config)
        handle = instances.temporary(storage.pk, "fake", {**plain, "port": 2121})
        handle.driver.refresh()
        self.assertEqual(stored(storage, "refresh_token"), "refresh-0")  # nothing yet
        rest = instances.apply_temporary(handle)
        self.assertEqual(rest, {})
        self.assertEqual(stored(storage, "refresh_token"), "refresh-1")
        self.assertEqual(live.config["refresh_token"], "refresh-1")

    def test_a_token_of_the_form_goes_back_to_the_form(self):
        storage = make_storage()
        plain, _ = config.decode(FakeDriver, storage.config)
        handle = instances.temporary(storage.pk, "fake", {**plain, "refresh_token": "typed-in"})
        handle.driver.refresh()
        rest = instances.apply_temporary(handle)
        self.assertEqual(rest["refresh_token"], "refresh-1")
        self.assertEqual(stored(storage, "refresh_token"), "refresh-0")

    def test_what_is_not_secret_goes_back_to_the_form(self):
        storage = make_storage()
        FakeDriver.learn_host_key = True
        plain, _ = config.decode(FakeDriver, storage.config)
        handle = instances.temporary(storage.pk, "fake", {**plain, "endpoint": "https://another.example"})
        handle.driver.check()
        self.assertEqual(instances.apply_temporary(handle), {"host_key": "ssh-ed25519 SHA256:fake"})
        self.assertIsNone(stored(storage, "host_key"))

    def test_new_storages_keep_everything_for_the_form(self):
        handle = instances.temporary(None, "fake", {"endpoint": "x", "refresh_token": "r"})
        handle.driver.refresh()
        self.assertEqual(instances.apply_temporary(handle)["refresh_token"], "refresh-1")


def plain(storage):
    values, _ = config.decode(FakeDriver, storage.config)
    return values


class SharedTokenTests(FakeTypeMixin, TestCase):
    """The open instances of a storage (the live one, a temporary one testing the form, an old one finishing its
    requests) share one lock for their tokens, and hand each other the tokens they refresh at once: a refresh
    token that works once (Baidu's, 115's) is not sent twice, and a driver never reads half of a change."""

    def test_one_token_lock_per_storage(self):
        storage = make_storage()
        live = instances.get(storage)
        temporary = instances.temporary(storage.pk, "fake", plain(storage))
        self.assertIs(temporary.driver.token_lock, live.token_lock)
        self.assertIsNot(instances.get(make_storage("other")).token_lock, live.token_lock)
        self.assertIsNot(instances.temporary(None, "fake", {"endpoint": "x"}).driver.token_lock, live.token_lock)

    def test_adopting_waits_for_a_driver_reading_its_tokens(self):
        storage = make_storage()
        driver = instances.get(storage)
        handle = instances.live(storage.pk)
        reading, read_on = threading.Event(), threading.Event()
        seen = []

        def reader():  # as Baidu's _token(): the access token, then its expiry, under the token lock
            with driver.token_lock:
                token = driver.config["token"]
                reading.set()
                read_on.wait(5)
                seen.append((token, driver.config.get("expires_at")))

        thread = threading.Thread(target=reader)
        thread.start()
        reading.wait(5)
        adopter = threading.Thread(target=handle.adopt, args=({"token": "access-9", "expires_at": 1009},
                                                              {"token": "access-0", "expires_at": None}))
        adopter.start()
        adopter.join(0.2)
        self.assertTrue(adopter.is_alive())  # waits for the reader
        read_on.set()
        thread.join(5)
        adopter.join(5)
        self.assertEqual(seen, [("access-0", None)])
        self.assertEqual((driver.config["token"], driver.config["expires_at"]), ("access-9", 1009))

    def test_a_temporary_instances_tokens_reach_the_live_one_at_once(self):
        storage = make_storage()
        live = instances.get(storage)
        temporary = instances.temporary(storage.pk, "fake", {**plain(storage), "port": 2121})
        temporary.driver.refresh()  # 测试连接 used the stored refresh token: the live instance's is spent too
        self.assertEqual((live.config["token"], live.config["refresh_token"], live.config["expires_at"]),
                         ("access-1", "refresh-1", 1001))
        self.assertEqual(stored(storage, "refresh_token"), "refresh-0")  # stored afterwards, as before
        self.assertEqual(instances.apply_temporary(temporary), {})
        self.assertEqual(stored(storage, "refresh_token"), "refresh-1")

    def test_the_live_instances_tokens_reach_a_temporary_one(self):
        storage = make_storage()
        live = instances.get(storage)
        temporary = instances.temporary(storage.pk, "fake", plain(storage))
        live.refresh()
        self.assertEqual((temporary.driver.config["token"], temporary.driver.config["refresh_token"]),
                         ("access-1", "refresh-1"))
        self.assertEqual(instances.apply_temporary(temporary), {})  # not its own: nothing for the form

    def test_an_old_instance_gets_the_new_ones_tokens(self):
        storage = make_storage()
        instances.get(storage)
        old = instances.live(storage.pk)
        self.addCleanup(old.close)
        StorageSource.objects.filter(pk=storage.pk).update(config_revision=2)  # qps changed, tokens kept
        storage.refresh_from_db()
        with mock.patch.object(instances, "_retire"):  # still finishing its requests
            new = instances.get(storage)
        new.refresh()
        self.assertEqual(old.driver.config["refresh_token"], "refresh-1")

    def test_tokens_go_only_to_instances_that_have_the_ones_they_replace(self):
        storage = make_storage()
        live = instances.get(storage)
        typed = instances.temporary(storage.pk, "fake", {**plain(storage), "refresh_token": "typed-in"})
        typed.driver.refresh()  # with the token typed into the form: not the live instance's account, maybe
        self.assertEqual((live.config["token"], live.config["refresh_token"]), ("access-0", "refresh-0"))
        live.refresh()
        self.assertEqual((typed.driver.config["token"], typed.driver.config["refresh_token"]),
                         ("access-1", "refresh-1"))

    def test_what_is_not_secret_stays_with_its_instance(self):
        storage = make_storage()
        live = instances.get(storage)
        FakeDriver.learn_host_key = True
        temporary = instances.temporary(storage.pk, "fake", {**plain(storage), "endpoint": "https://another.example"})
        temporary.driver.check()  # the host key of the server in the form: the admin saves it, or not
        self.assertIsNone(live.config.get("host_key"))


class SharedTokenThreadTests(FakeTypeMixin, TransactionTestCase):
    def test_two_instances_renew_with_a_single_use_refresh_token_once(self):
        storage = make_storage()
        live = instances.get(storage)
        temporary = instances.temporary(storage.pk, "fake", plain(storage))
        barrier = threading.Barrier(2)
        errors = []

        def renew(driver):
            barrier.wait()
            try:
                driver.renew("access-0")
            except Exception as error:  # noqa: BLE001
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=renew, args=(driver,)) for driver in (live, temporary.driver)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(FakeDriver.rotations, 1)
        self.assertEqual(temporary.driver.config["token"], live.config["token"])


class ThreadedSaveTests(FakeTypeMixin, TransactionTestCase):
    """Refreshes from many threads at once all end up stored (each its own connection and lock)."""

    def test_concurrent_refreshes(self):
        storage = make_storage()
        driver = instances.get(storage)
        errors = []

        def worker():
            try:
                driver.refresh()
            except Exception as error:  # noqa: BLE001
                errors.append(error)
            finally:
                from django.db import connection

                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        final = stored(storage, "refresh_token")
        self.assertRegex(final, r"^refresh-[1-6]$")
        self.assertEqual(driver.config["refresh_token"] is not None, True)
