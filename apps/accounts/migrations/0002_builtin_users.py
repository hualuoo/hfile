"""The two built-in users: the guest (whoever isn't logged in) and the template (what users created
by single sign-on start with). They can't log in: their password is unusable, and the login
backend refuses them anyway (apps/accounts/backends.py)."""

from django.contrib.auth.hashers import make_password
from django.db import migrations

BUILTIN = [("guest", "guest", "匿名用户"), ("template", "template", "虚拟新用户")]


def create(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for kind, username, nickname in BUILTIN:
        if not User.objects.filter(kind=kind).exists():
            User.objects.create(username=username, nickname=nickname, kind=kind, password=make_password(None))


def remove(apps, schema_editor):
    apps.get_model("accounts", "User").objects.filter(kind__in=[kind for kind, _, _ in BUILTIN]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]
    operations = [migrations.RunPython(create, remove)]
