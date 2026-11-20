"""What the siteconf tests share."""

from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

User = get_user_model()


def users():
    """(superuser, staff who isn't a superuser, a user who isn't an admin)."""
    boss = User.objects.create_superuser("boss", password="pw-boss-2026")
    staff = User.objects.create_user("staff", password="pw-staff-2026", is_staff=True)
    alice = User.objects.create_user("alice", password="pw-alice-2026")
    return boss, staff, alice


def png(width=64, height=64, name="logo.png", color=(30, 110, 220)):
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")
