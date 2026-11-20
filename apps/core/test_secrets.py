from django.test import SimpleTestCase, override_settings

from apps.core import secrets


class SecretsTests(SimpleTestCase):
    def test_round_trip(self):
        stored = secrets.encrypt("ak/sk 密钥 = 1")
        self.assertTrue(stored.startswith("enc:"))
        self.assertNotIn("密钥", stored)
        self.assertNotEqual(stored, secrets.encrypt("ak/sk 密钥 = 1"))  # a new IV each time
        self.assertEqual(secrets.decrypt(stored), "ak/sk 密钥 = 1")
        self.assertTrue(secrets.is_set(stored))

    def test_not_set(self):
        self.assertEqual(secrets.encrypt(""), "")
        self.assertEqual(secrets.decrypt(""), "")
        self.assertEqual(secrets.decrypt(None), "")
        self.assertFalse(secrets.is_set(""))

    def test_another_secret_key(self):
        stored = secrets.encrypt("token")
        with override_settings(SECRET_KEY="another-key-" + "x" * 40):
            with self.assertRaises(secrets.UnreadableSecret):
                secrets.decrypt(stored)
        self.assertEqual(secrets.decrypt(stored), "token")

    def test_not_ours(self):
        for stored in ["plain text", "enc:garbage", "enc:"]:
            with self.assertRaises(secrets.UnreadableSecret, msg=stored):
                secrets.decrypt(stored)
        with self.assertRaises(TypeError):
            secrets.encrypt(None)
