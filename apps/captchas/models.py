"""The image captchas handed out (Challenge): which form, the answer, until when."""

from django.db import models
from django.utils.translation import gettext_lazy

LENGTH = 4  # characters in a picture


class Scene(models.TextChoices):
    """The forms that may ask for a captcha. 登录 covers the Django admin's login page too."""

    LOGIN = "login", gettext_lazy("Log in")


class Challenge(models.Model):
    """One captcha picture that was handed out: its answer, for one form, until it expires.

    Each is good for a single try: apps.captchas.service.verify() deletes it, right answer or not.
    Expired ones are deleted whenever a new one is handed out.
    """

    key = models.CharField(max_length=32, unique=True)
    scene = models.CharField(max_length=30, choices=Scene.choices)
    answer = models.CharField(max_length=LENGTH)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Captcha")

    def __str__(self):
        return f"{self.get_scene_display()} {self.key}"
