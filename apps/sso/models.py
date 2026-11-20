"""Single sign-on: the providers people log in with (OAuth 2 / OpenID Connect, the authorization-code
flow with PKCE and a userinfo endpoint), and which of their accounts belongs to which user.

A provider account is bound to a user by its stable id at the provider (Identity), never by a name:
whoever controls an account there whose name equals a local username would otherwise log in as that
user (the admin included). Admins and users bind accounts explicitly; a new user is made
only while 允许单点登录自动注册新用户 is on (siteconf), from the template user.
"""

import hashlib

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy

from apps.core import secrets

CODE_PATTERN = r"[a-z0-9_-]{1,32}"


class TokenAuth(models.TextChoices):
    # RFC 6749's client_secret_post: the most widely accepted (GitHub accepts only this).
    POST = "post", gettext_lazy("In the request's form (client_secret_post)")
    # OpenID Connect's default; some providers accept only this.
    BASIC = "basic", gettext_lazy("HTTP Basic authentication (client_secret_basic)")


class Provider(models.Model):
    """One button on the login page. `code` is its address, /sso/<code>/login, and part of the callback
    registered at the provider. The client secret is stored encrypted (apps.core.secrets) and never sent back."""

    code = models.CharField(
        gettext_lazy("Short name"), max_length=32, unique=True,
        help_text=gettext_lazy("The name used in addresses: /sso/<short-name>/login. Lowercase letters, digits, - and _ only."),
    )
    name = models.CharField(gettext_lazy("Name"), max_length=50, help_text=gettext_lazy("The name shown when hovering over the login button."))
    icon = models.TextField(
        gettext_lazy("Icon"), blank=True,
        help_text=gettext_lazy("An http(s) address, or a data:image/… address (PNG, JPEG, GIF, WebP, "
                               "SVG), at most 64 KB; when empty, the first letter of the name is shown."),
    )
    client_id = models.CharField("Client ID", max_length=255)
    client_secret = models.TextField("Client Secret", blank=True, editable=False)
    auth_url = models.CharField(gettext_lazy("Authorization endpoint"), max_length=1000)
    token_url = models.CharField(gettext_lazy("Token endpoint"), max_length=1000)
    userinfo_url = models.CharField(gettext_lazy("Userinfo endpoint"), max_length=1000)
    scope = models.CharField("Scope", max_length=500, blank=True, help_text=gettext_lazy("The scopes asked for, separated by spaces."))
    token_auth = models.CharField(
        gettext_lazy("How the Client Secret is sent"), max_length=8, choices=TokenAuth.choices, default=TokenAuth.POST,
    )
    id_field = models.CharField(
        gettext_lazy("User ID field"), max_length=200, default="id",
        help_text=gettext_lazy("Where the user info has the unique ID that never changes: sub for OpenID Connect, id for GitHub and Gitee."),
    )
    username_field = models.CharField(
        gettext_lazy("Username field"), max_length=200, blank=True, help_text=gettext_lazy("The field for the username of users registered automatically (another one is made up when it's taken)."),
    )
    nickname_field = models.CharField(gettext_lazy("Nickname field"), max_length=200, blank=True, help_text=gettext_lazy("The nickname of users registered automatically."))
    enabled = models.BooleanField(gettext_lazy("Enabled"), default=True, help_text=gettext_lazy("When not enabled, it isn't shown on the login page and can't be used to log in."))
    order = models.IntegerField(gettext_lazy("Sort"), default=0, help_text=gettext_lazy("The order on the login page, smallest first."))
    created_at = models.DateTimeField(gettext_lazy("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(gettext_lazy("Modified"), auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = verbose_name_plural = gettext_lazy("Single sign-on provider")

    def __str__(self):
        return _("%(name)s (%(code)s)") % {"name": self.name, "code": self.code}

    def secret(self):
        """The client secret in the clear, "" when none is set. Raises apps.core.secrets.UnreadableSecret."""
        return secrets.decrypt(self.client_secret)

    def set_secret(self, value):
        self.client_secret = secrets.encrypt(value)


def key_of(external_id):
    """What identities are looked up by: the SHA-256 of the id, in hex. Ids at providers are compared
    exactly (an OpenID Connect sub is case-sensitive), and MySQL's collation compares text without case
    or accents: two people whose ids differ only in case would otherwise be one user there."""
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()


class Identity(models.Model):
    """An account at a provider (its stable id there) that logs in as `user`. One per provider and user.
    Never for the built-in users (the guest and the template): they don't log in."""

    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="identities", verbose_name=gettext_lazy("Provider"))
    external_id = models.CharField(gettext_lazy("User ID at the provider"), max_length=255)
    external_key = models.CharField(max_length=64, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sso_identities", verbose_name=gettext_lazy("User"),
    )
    created_at = models.DateTimeField(gettext_lazy("Linked at"), default=timezone.now)
    last_login_at = models.DateTimeField(gettext_lazy("Last login with it"), null=True, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Single sign-on link")
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_key"], name="sso_identity_unique_account",
                violation_error_message=gettext_lazy("This account is linked to another user."),
            ),
            models.UniqueConstraint(
                fields=["provider", "user"], name="sso_identity_one_per_provider",
                violation_error_message=gettext_lazy("This user has already linked an account of this provider."),
            ),
        ]

    def __str__(self):
        return f"{self.provider.code}:{self.external_id}"

    def save(self, *args, **kwargs):
        self.external_key = key_of(self.external_id)
        if kwargs.get("update_fields") is not None and "external_id" in kwargs["update_fields"]:
            kwargs["update_fields"] = {*kwargs["update_fields"], "external_key"}
        super().save(*args, **kwargs)
