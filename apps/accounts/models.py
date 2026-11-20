from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy, gettext_noop

from apps.ipgeo.sources import LOCATION_MAX

from .permissions import ADMIN_DEFAULTS, validate_permissions
from .validators import NICKNAME_MAX, USERNAME_MAX, validate_nickname


class Kind(models.TextChoices):
    NORMAL = "normal", gettext_lazy("Regular user")
    # Whoever isn't logged in: its grants are what visitors may do, and switching it off (is_active)
    # turns anonymous access off.
    GUEST = "guest", gettext_lazy("Anonymous user")
    # What users created by single sign-on start with: a copy of its default permissions and
    # grants.
    TEMPLATE = "template", gettext_lazy("New user template")


# The usernames of the built-in users; no account of another kind can have them (whatever the case).
BUILTIN_USERNAMES = {Kind.GUEST: "guest", Kind.TEMPLATE: "template"}
# Stored as they are, like the migration does (they can't be changed); the users list shows them translated.
BUILTIN_NICKNAMES = {Kind.GUEST: gettext_noop("Anonymous user"), Kind.TEMPLATE: gettext_noop("New user template")}


class UserManager(DjangoUserManager):
    def create_superuser(self, username=None, email=None, password=None, **extra_fields):
        """The installer's first admin and `createsuperuser`: with every permission but the two that
        bypass the rules on the storage sources created later (permissions.ADMIN_DEFAULTS)."""
        extra_fields.setdefault("default_permissions", list(ADMIN_DEFAULTS))
        return super().create_superuser(username, email, password, **extra_fields)

    def normal(self):
        """The accounts people log in with: not the guest or the template."""
        return self.filter(kind=Kind.NORMAL)

    def username_taken(self, username, *, exclude=None):
        """Whether another account (not the one of pk `exclude`) has `username` as the unique indexes
        see it: without case (Lower("username"), for SQLite), and as the column's collation compares
        (=): MySQL's utf8mb4_0900_ai_ci also takes ß for ss, æ for ae and é for e, which iexact (LIKE,
        character by character) doesn't, so the save would fail on the index instead (ACCT-01)."""
        others = self.filter(Q(username__iexact=username) | Q(username=username))
        if exclude is not None:
            others = others.exclude(pk=exclude)
        return others.exists()

    def guest(self):
        return self._builtin(Kind.GUEST)

    def template(self):
        return self._builtin(Kind.TEMPLATE)

    def _builtin(self, kind):
        """The built-in user of `kind`. accounts/migrations/0002 creates both; this makes the row
        again when it is gone (a test database flushed between tests)."""
        user = self.filter(kind=kind).order_by("pk").first()
        if user is not None:
            return user
        user, _ = self.get_or_create(
            username=BUILTIN_USERNAMES[kind],
            defaults={"kind": kind, "nickname": BUILTIN_NICKNAMES[kind], "password": make_password(None)},
        )
        if user.kind != kind:
            raise RuntimeError(f"the account {user.username!r} isn't the built-in {kind} user")
        return user


class User(AbstractUser):
    """Django's user, as HFile keeps it: a username that logs in, an optional nickname to show, an
    optional email address, and the IP of the last login. No first or last name.

    A custom user model has to exist before the first migration; adding one later means rewriting
    every foreign key to the user table. Admins are staff (apps/core/permissions.py); the installer's first
    admin is a superuser. is_active is 启用 on the user pages: a user who isn't can't log in.
    """

    first_name = None
    last_name = None
    nickname = models.CharField(
        gettext_lazy("Nickname"), max_length=NICKNAME_MAX, blank=True, validators=[validate_nickname],
        help_text=gettext_lazy("Only for display, and others may have the same one; logging in uses the username."),
    )
    last_login_ip = models.GenericIPAddressField(gettext_lazy("Last login IP"), null=True, blank=True, unpack_ipv4=True)
    # Its IP 归属地 (apps/ipgeo/places.py): looked up at login while that is turned on; "" otherwise.
    last_login_location = models.CharField(gettext_lazy("Last login IP location"), max_length=LOCATION_MAX, blank=True)
    kind = models.CharField(
        gettext_lazy("Type"), max_length=16, choices=Kind.choices, default=Kind.NORMAL, editable=False, db_index=True,
        help_text=gettext_lazy("The anonymous user's permissions are what people who aren't logged "
                               "in can do; the new user template's are what new single sign-on "
                               "users start with. These two users can't log in or be deleted."),
    )
    default_permissions = models.JSONField(
        gettext_lazy("Default permissions"), default=list, blank=True, validators=[validate_permissions],
        help_text=gettext_lazy("The permissions this user gets on storage sources added later (existing ones "
                               "aren't affected). With none, new storage sources aren't given to this user."),
    )

    # One more each time the account is disabled: every session from before then stops working, also
    # after the account is enabled again (get_session_auth_hash). A changed password does the same.
    session_epoch = models.PositiveIntegerField(gettext_lazy("Login generation"), default=0, editable=False)
    # Two-step verification (apps/accounts/totp.py), for superusers: the TOTP secret, encrypted with
    # apps.core.secrets and never sent back ("" = off); the last time step a code was accepted for, so that
    # a code works once; and since when it is on.
    totp_secret = models.TextField(gettext_lazy("Two-step verification key"), blank=True, editable=False)
    totp_last_step = models.BigIntegerField(gettext_lazy("Last two-step verification time step used"), null=True, blank=True, editable=False)
    totp_enabled_at = models.DateTimeField(gettext_lazy("Two-step verification turned on at"), null=True, blank=True, editable=False)

    objects = UserManager()

    REQUIRED_FIELDS = []  # createsuperuser asks for the username and the password only

    class Meta(AbstractUser.Meta):
        verbose_name = verbose_name_plural = gettext_lazy("User")
        constraints = [
            # Usernames compared without case, as logging in does (apps/accounts/backends.py). MySQL's
            # collation (utf8mb4_0900_ai_ci) makes `username` unique that way already; SQLite needs this.
            models.UniqueConstraint(
                Lower("username"),
                name="accounts_user_username_ci_unique",
                violation_error_message=gettext_lazy("This username is taken."),
            ),
        ]

    def save(self, *args, **kwargs):
        # Disabling ends the sessions the account has (see session_epoch), wherever it is done: the
        # user management, the Django admin, a command.
        update_fields = kwargs.get("update_fields")
        writes_active = update_fields is None or "is_active" in update_fields
        if self.pk is not None and not self.is_active and writes_active and self._active_in_database():
            self.session_epoch += 1
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "session_epoch"}
        super().save(*args, **kwargs)

    def _active_in_database(self):
        return type(self)._default_manager.filter(pk=self.pk, is_active=True).exists()

    def get_session_auth_hash(self):
        """Django's HMAC of the password hash, with the session epoch in it once there is one: a
        session lasts while neither changes. Epoch 0 gives Django's own hash."""
        return self._session_hash()

    def get_session_auth_fallback_hash(self):
        for secret in settings.SECRET_KEY_FALLBACKS:
            yield self._session_hash(secret)

    def _session_hash(self, secret=None):
        value = self.password if not self.session_epoch else f"{self.password}\x00{self.session_epoch}"
        key_salt = "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash"
        return salted_hmac(key_salt, value, secret=secret, algorithm="sha256").hexdigest()

    @property
    def totp_enabled(self):
        return bool(self.totp_secret)

    @property
    def is_builtin(self):
        """The guest or the template: they never log in and can't be deleted."""
        return self.kind != Kind.NORMAL

    @property
    def display_name(self):
        return self.nickname or self.username

    def get_full_name(self):
        return self.display_name

    def get_short_name(self):
        return self.display_name


class LoginLog(models.Model):
    """登录日志: one login attempt that was checked, and how it went. Never a password.

    Written while SiteSettings.login_log is on (apps/accounts/loginlog.py). `username` is what was typed
    (cut to its length); `user` the account it matched, a link that goes when the account is deleted.
    `reason` and `source` are codes of loginlog.REASONS and loginlog.SOURCES, `ip_location` one of the ipgeo
    app's (apps/ipgeo/places.py).
    """

    class Result(models.TextChoices):
        SUCCESS = "success", gettext_lazy("Succeeded")
        FAILURE = "failure", gettext_lazy("Failed")

    created_at = models.DateTimeField(gettext_lazy("Time"), default=timezone.now, db_index=True)
    username = models.CharField(gettext_lazy("Username entered"), max_length=USERNAME_MAX, blank=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name=gettext_lazy("User"))
    ip = models.GenericIPAddressField("IP", null=True, blank=True, unpack_ipv4=True)
    # Its IP 归属地 (ipgeo app): looked up when the entry is written while that is turned on, or filled in
    # later (补全 on /manage/#ip-geo).
    ip_location = models.CharField(gettext_lazy("IP location"), max_length=LOCATION_MAX, blank=True)
    user_agent = models.CharField(gettext_lazy("Browser"), max_length=500, blank=True)
    result = models.CharField(gettext_lazy("Result"), max_length=10, choices=Result.choices)
    reason = models.CharField(gettext_lazy("Reason"), max_length=32, blank=True)
    source = models.CharField(gettext_lazy("Where"), max_length=16, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Login log")

    def __str__(self):
        return f"{self.username} {self.get_result_display()}"
