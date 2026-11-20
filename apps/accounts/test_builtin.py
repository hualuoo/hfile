from django.contrib import admin
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase

from .models import Kind, User
from .permissions import ADMIN_DEFAULTS, ALL, REQUIRES, Permission, check_requires, clean_permissions, help_for, unmet, usable
from .validators import check_username


class BuiltinUserTests(TestCase):
    def test_both_exist_after_migrating(self):
        guest, template = User.objects.guest(), User.objects.template()
        self.assertEqual((guest.kind, guest.username, guest.nickname), (Kind.GUEST, "guest", "匿名用户"))
        self.assertEqual((template.kind, template.username), (Kind.TEMPLATE, "template"))
        self.assertTrue(guest.is_builtin and template.is_builtin)
        self.assertFalse(guest.has_usable_password())
        self.assertFalse(User.objects.normal().filter(pk__in=[guest.pk, template.pk]).exists())

    def test_made_again_when_gone(self):
        User.objects.filter(kind=Kind.GUEST).delete()  # as a flushed test database has it
        guest = User.objects.guest()
        self.assertEqual(guest.kind, Kind.GUEST)
        self.assertEqual(User.objects.guest().pk, guest.pk)

    def test_never_log_in(self):
        guest = User.objects.guest()
        guest.set_password("a password someone set")
        guest.save()
        self.assertIsNone(authenticate(username="guest", password="a password someone set"))
        response = self.client.post(
            "/api/v1/session/", {"username": "guest", "password": "a password someone set"}, content_type="application/json"
        )
        self.assertNotIn(response.status_code, (200, 201), response.content)

    def test_names_are_reserved(self):
        for name in ["guest", "Guest", "TEMPLATE"]:
            with self.assertRaises(ValidationError):
                check_username(name)
        check_username("guest2")

    def test_django_admin_cannot_delete_them(self):
        model_admin = admin.site._registry[User]
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("boss", password="x" * 12)
        self.assertFalse(model_admin.has_delete_permission(request, User.objects.guest()))
        self.assertTrue(model_admin.has_delete_permission(request, User.objects.create_user("someone")))
        model_admin.delete_queryset(request, User.objects.all())
        # The built-ins stay, and so does the admin doing it (apps/accounts/managing.py).
        self.assertEqual(sorted(User.objects.values_list("username", flat=True)), ["boss", "guest", "template"])


class PermissionTests(TestCase):
    def test_clean(self):
        self.assertEqual(clean_permissions(["upload", "new_folder", "upload"]), ["new_folder", "upload"])
        self.assertEqual(clean_permissions(None), [])
        self.assertEqual(len(ALL), 15)
        self.assertNotIn(Permission.IGNORE_HIDDEN, ADMIN_DEFAULTS)
        self.assertNotIn(Permission.IGNORE_PASSWORD, ADMIN_DEFAULTS)
        for bad in ["upload", ["upload", "fly"], [1], {"upload": True}]:
            with self.assertRaises(ValidationError, msg=repr(bad)):
                clean_permissions(bad)

    def test_what_a_permission_needs_first(self):
        # D55: one of these is ticked first.
        self.assertEqual(REQUIRES, {
            "preview": ("download",), "copy_download_link": ("download",), "generate_link": ("download",),
            "generate_short_link": ("download",), "custom_share_key": ("create_share_link",),
        })
        self.assertTrue(set(ADMIN_DEFAULTS) >= {code for needs in REQUIRES.values() for code in needs})
        self.assertEqual(unmet(ADMIN_DEFAULTS), [])
        guest = ["upload", "preview", "copy_download_link", "generate_link"]  # no 下载
        self.assertEqual(unmet(guest), ["preview", "copy_download_link", "generate_link"])
        self.assertEqual(usable(guest), ["upload"])
        self.assertEqual(usable(["copy_download_link", "generate_short_link", "custom_share_key", "upload"]), ["upload"])
        with self.assertRaises(ValidationError) as caught:
            check_requires(["preview", "copy_download_link", "generate_link", "custom_share_key"])
        self.assertEqual(caught.exception.messages, [
            "「预览」要先有「下载」的权限。", "「复制下载链接」要先有「下载」的权限。", "「获取直链」要先有「下载」的权限。",
            "「自定义分享链接 key」要先有「创建分享链接」的权限。",
        ])
        check_requires(["preview", "download", "copy_download_link", "generate_link", "generate_short_link"])
        self.assertTrue(help_for("preview").startswith("要先勾选「下载」。在网页上预览"))
        self.assertEqual(help_for("generate_link"), "要先勾选「下载」。")
        self.assertEqual(help_for("custom_share_key"), "要先勾选「创建分享链接」。创建分享链接时可以自己定链接里的 key，而不是由系统生成。")
        self.assertEqual(help_for("rename"), "")

    def test_model_field(self):
        user = User(username="perm-user", default_permissions=["upload", "new_folder"])
        with self.assertRaises(ValidationError):
            user.full_clean(exclude=["password"])  # not in the canonical order
        user.default_permissions = clean_permissions(user.default_permissions)
        user.full_clean(exclude=["password"])
