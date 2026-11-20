"""Driver params on the form and in the database (apps/storages/config.py)."""

import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.core import secrets as core_secrets
from apps.storages import config
from apps.storages.drivers import GROUPS, driver_class, types

from .fakes import FakeDriver, FakeTypeMixin


class CleanTests(FakeTypeMixin, SimpleTestCase):
    def clean(self, submitted, **kwargs):
        return config.clean(FakeDriver, submitted, **kwargs)

    def errors(self, submitted, **kwargs):
        with self.assertRaises(config.ConfigError) as caught:
            self.clean(submitted, **kwargs)
        return caught.exception.errors

    def test_types_and_defaults(self):
        cleaned = self.clean({"endpoint": "  https://x  ", "port": "2121", "passive": False, "mode": "b",
                              "notes": " a\r\nb \n"})
        self.assertEqual(cleaned["endpoint"], "https://x")
        self.assertEqual(cleaned["port"], 2121)
        self.assertIs(cleaned["passive"], False)
        self.assertEqual(cleaned["mode"], "b")
        self.assertEqual(cleaned["notes"], "a\nb")
        cleaned = self.clean({"endpoint": "x"})
        self.assertEqual((cleaned["port"], cleaned["passive"], cleaned["mode"]), (21, True, "a"))
        self.assertIsNone(cleaned["token"])
        self.assertEqual(self.clean({"endpoint": "x", "port": ""})["port"], 21)  # emptied: the default

    def test_messages_per_field(self):
        errors = self.errors({"endpoint": "", "port": "x", "passive": "yes", "mode": "c", "nope": 1})
        self.assertEqual(errors["endpoint"], "请填写「地址」。")
        self.assertEqual(errors["port"], "请填一个整数。")
        self.assertEqual(errors["passive"], "应该是开或关。")
        self.assertEqual(errors["mode"], "请从列表里选一项。")
        self.assertEqual(errors["nope"], "这种存储没有这个参数。")
        self.assertEqual(self.errors({"endpoint": "x", "port": 0})["port"], "不能小于 1。")
        self.assertEqual(self.errors({"endpoint": "x", "port": 70000})["port"], "不能大于 65535。")
        self.assertEqual(self.errors({"endpoint": "x", "port": True})["port"], "请填一个整数。")
        self.assertIn("控制字符", self.errors({"endpoint": "a\nb"})["endpoint"])
        self.assertEqual(self.errors({"endpoint": 5})["endpoint"], "要写成文字。")
        self.assertIn("最多", self.errors({"endpoint": "x" * 3000})["endpoint"])

    def test_the_drivers_own_rules(self):
        self.assertEqual(self.errors({"endpoint": "bad://"}), {"endpoint": "地址不对。"})

    def test_secrets_are_kept_unless_typed_or_cleared(self):
        stored = {"endpoint": "x", "token": "old-token", "refresh_token": "old-refresh"}
        cleaned = self.clean({"endpoint": "x", "token": "", "refresh_token": None}, stored=stored)
        self.assertEqual((cleaned["token"], cleaned["refresh_token"]), ("old-token", "old-refresh"))
        cleaned = self.clean({"endpoint": "x", "token": " new "}, stored=stored)
        self.assertEqual(cleaned["token"], " new ")  # as typed (STOR-14)
        cleaned = self.clean({"endpoint": "x"}, stored=stored, draft={"refresh_token": "from-oauth"})
        self.assertEqual(cleaned["refresh_token"], "from-oauth")
        cleaned = self.clean({"endpoint": "x"}, stored=stored, clear={"token"})
        self.assertIsNone(cleaned["token"])
        self.assertIn("只能清除", self.errors({"endpoint": "x"}, clear={"endpoint"})["endpoint"])

    def test_passwords_keep_their_spaces(self):
        """STOR-14: an FTP, SFTP or WebDAV password may begin or end with a space; cut, the login failed.
        Spaces alone still mean "left empty", and the other kinds are still trimmed."""
        stored = {"endpoint": "x", "token": "old-token"}
        cleaned = self.clean({"endpoint": " x ", "token": "  pass word  ", "host_key": " ab "}, stored=stored)
        self.assertEqual((cleaned["token"], cleaned["endpoint"], cleaned["host_key"]), ("  pass word  ", "x", "ab"))
        self.assertEqual(self.clean({"endpoint": "x", "token": "   "}, stored=stored)["token"], "old-token")
        self.assertIn("控制字符", self.errors({"endpoint": "x", "token": " a\nb "})["token"])

    def test_partial_keeps_what_is_left_out(self):
        stored = {"endpoint": "kept", "port": 2222, "mode": "b"}
        cleaned = self.clean({"port": 23}, stored=stored, partial=True)
        self.assertEqual((cleaned["endpoint"], cleaned["port"], cleaned["mode"]), ("kept", 23, "b"))
        cleaned = self.clean({"endpoint": "x"}, stored=stored)  # a full form: left out means the default
        self.assertEqual(cleaned["port"], 21)

    def test_later_lets_required_params_wait(self):
        """The add wizard's second step: required params of its later steps may stay empty for now."""
        self.assertIn("endpoint", self.errors({"port": 23}))
        cleaned = self.clean({"port": 23}, later={"endpoint"})
        self.assertIsNone(cleaned["endpoint"])
        self.assertEqual((cleaned["port"], cleaned["mode"]), (23, "a"))
        self.assertIn("port", self.errors({"port": "x"}, later={"port"}))  # a value typed is still checked


class StoredFormTests(FakeTypeMixin, SimpleTestCase):
    def test_secrets_are_encrypted_and_write_only(self):
        stored = config.encode(FakeDriver, {"endpoint": "x", "token": "t0ken", "refresh_token": None, "expires_at": 123})
        self.assertTrue(stored["token"].startswith("enc:"))
        self.assertNotIn("t0ken", str(stored))
        self.assertEqual(stored["refresh_token"], "")
        self.assertTrue(stored["expires_at"].startswith("enc:"))  # a secret number is stored as its text
        plain, unreadable = config.decode(FakeDriver, stored)
        self.assertEqual((plain["token"], plain["refresh_token"], plain["expires_at"]), ("t0ken", None, 123))
        self.assertEqual(unreadable, [])
        values, state = config.shown(FakeDriver, stored)
        self.assertEqual(state, {"token": "set", "refresh_token": "unset", "expires_at": "set"})
        self.assertNotIn("token", values)
        self.assertEqual(values["endpoint"], "x")

    def test_what_is_left_out_is_stored_with_its_default(self):
        # Code that writes a config without clean() (a copy, a test, a command) gets the defaults too:
        # an S3 storage without is_private must stay private, not read as False (a public bucket).
        s3 = driver_class("s3")
        stored = config.encode(s3, {"endpoint": "http://127.0.0.1:9000", "bucket": "b"})
        self.assertIs(stored["is_private"], True)
        stored = config.encode(s3, {"endpoint": "http://127.0.0.1:9000", "bucket": "b", "is_private": None})
        self.assertIs(stored["is_private"], True)
        stored = config.encode(s3, {"endpoint": "http://127.0.0.1:9000", "bucket": "b", "is_private": False})
        self.assertIs(stored["is_private"], False)

    def test_a_changed_secret_key_makes_them_unreadable(self):
        stored = config.encode(FakeDriver, {"endpoint": "x", "token": "t0ken"})
        with override_settings(SECRET_KEY="another-key-" + "x" * 40):
            plain, unreadable = config.decode(FakeDriver, stored)
            self.assertNotIn("token", plain)
            self.assertEqual(unreadable, ["token"])
            self.assertEqual(config.shown(FakeDriver, stored)[1]["token"], "unreadable")
        self.assertEqual(core_secrets.decrypt(stored["token"]), "t0ken")


class EveryDriverTests(SimpleTestCase):
    """The 18 drivers' params are all of kinds the form knows."""

    def test_kinds(self):
        for type_ in types():
            cls = driver_class(type_)
            for param in cls.params:
                with self.subTest(type=type_, param=param.key):
                    self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                    if param.kind == "select":
                        self.assertTrue(param.options)
                    if param.secret:
                        self.assertIn(param.kind, ("password", "textarea", "number"))
                    if param.default is not None and param.kind != "bool":
                        config.check_value(param, param.default)

    def test_groups(self):
        """Every type is in one group of the form's list, and every type a group names exists."""
        members = [type_ for _, group in GROUPS.values() for type_ in group]
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(set(members), set(types()))

    def test_the_add_wizards_params(self):
        """setup_params name params of the driver that lists them (a subclass may have fewer); what 授权 fills
        are secrets, never asked for in the second step; that step always asks for something."""
        for type_ in types():
            cls = driver_class(type_)
            keys = {param.key: param for param in cls.params}
            with self.subTest(type=type_):
                self.assertTrue(config.setup_keys(cls))
                self.assertFalse(set(config.setup_keys(cls)) & set(cls.auth_params))
                for key in cls.auth_params:
                    self.assertTrue(keys[key].secret, key)
                owner = next(klass for klass in cls.__mro__ if "setup_params" in vars(klass))
                if owner is cls and cls.setup_params is not None:
                    self.assertLessEqual(set(cls.setup_params), set(keys))


class LocalRootTests(SimpleTestCase):
    """A local storage may not serve the site's own files (config.toml holds the secret key)."""

    def test_refused_over_the_sites_folders(self):
        cls = driver_class("local")
        own = Path(settings.CONFIG_FILE).resolve().parent
        for folder in (own, own.parent, Path(settings.BASE_DIR)):
            with self.subTest(folder=folder):
                with self.assertRaises(config.ConfigError) as caught:
                    config.clean(cls, {"root": str(folder)})
                self.assertIn("HFile 自己的", caught.exception.errors["root"])
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(config.clean(cls, {"root": other})["root"], other)
