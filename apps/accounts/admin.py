from functools import cached_property

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext as _, gettext_lazy, ngettext

from apps.captchas import service as captchas
from apps.captchas.models import Scene
from apps.captchas.serializers import ANSWER_INPUT_MAX
from apps.core.throttles import login_allowed

from . import entry as login_entry
from . import login as steps
from . import loginlog, managing
from .models import User
from .validators import check_username


class AdminLoginForm(AdminAuthenticationForm):
    """The Django admin's login page: the same budget per address as POST /api/v1/session/
    (apps.core.throttles.LoginThrottle), the login captcha while the site asks for one, and no way round
    the site's later login steps (apps/accounts/login.py): an account that needs one logs in on the site
    first. As in the session API: the limit, then the captcha, then the password. While a secure
    login entry is set it logs nobody in (apps/accounts/entry.py): log in at the entry, then open the admin."""

    ENTRY_SET = gettext_lazy("A secure login entrance is set: log in to the site through it, then open the Django "
                             "admin. If you forgot the entrance, running hfile resetadmin on the server shows it.")

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.entry_set = bool(login_entry.current())
        self.captcha_required = captchas.required(Scene.LOGIN) and not self.entry_set
        if self.captcha_required:
            # Not required=True: verify() says what is missing, before anything else runs.
            self.fields["captcha"] = forms.CharField(label=_("Captcha"), required=False, max_length=ANSWER_INPUT_MAX)

    def clean(self):
        allowed, wait = login_allowed(self.request)
        if not allowed:
            loginlog.record_throttled(self.request, loginlog.typed_username(self.data), seconds=wait)
            raise ValidationError(ngettext("Too many logins. Please try again in %(wait)s second.", "Too many logins. Please try again in %(wait)s seconds.", wait) % {"wait": wait}, code="throttled")
        if self.entry_set:
            raise ValidationError(self.ENTRY_SET, code="entry")
        if self.captcha_required:
            try:
                captchas.verify(Scene.LOGIN, self.data.get("captcha_key"), self.data.get("captcha"))
            except captchas.CaptchaError as exc:
                if exc.code != "captcha_required":
                    loginlog.record(self.request, success=False, username=loginlog.typed_username(self.data), reason="captcha")
                self.add_error("captcha", ValidationError(exc.message, code=exc.code))
                return self.cleaned_data
        return super().clean()

    captcha_wait = 0  # seconds, when new_captcha is None

    @cached_property
    def new_captcha(self):
        """A picture for the page being shown (the one just answered, if any, is used up). It counts in
        the same budget as POST /api/v1/captcha/; over it there is none (None), and captcha_wait says
        for how long."""
        allowed, self.captcha_wait = captchas.issue_allowed(self.request)
        return captchas.issue(Scene.LOGIN) if allowed else None

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if steps.needs_steps(self.request, user):
            loginlog.record(self.request, success=False, username=loginlog.typed_username(self.data), user=user,
                            reason="needs_steps")
            raise ValidationError(_("This account has to log in through the site's login box "
                                    "(there is one more step), then open the Django admin."), code="steps")


admin.site.login_form = AdminLoginForm
admin.site.login_template = "accounts/admin_login.html"
admin.site.site_header = admin.site.site_title = gettext_lazy("HFile admin")
admin.site.index_title = gettext_lazy("Manage")


class HFileUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)

    def clean_username(self):
        username = User.normalize_username(self.cleaned_data["username"])
        check_username(username)
        self.cleaned_data["username"] = username
        return super().clean_username()  # Django's: no other account by that name, whatever its case


class HFileUserChangeForm(UserChangeForm):
    """The Django admin's page of a user. Who saves it is `current_user` (UserAdmin.get_form)."""

    current_user = None

    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"

    def clean_username(self):
        username = self.cleaned_data["username"]
        if username == self.instance.username:
            return username  # unchanged
        username = User.normalize_username(username)
        check_username(username)
        return username

    def clean(self):
        cleaned = super().clean()
        problem = admin_change_problem(self.current_user, self.instance, cleaned)
        if problem:
            raise ValidationError(problem)
        return cleaned


def admin_change_problem(actor, target, values):
    """Why the Django admin may not save `values` (is_active, is_staff, is_superuser) on `target`, or "".

    Only superusers change other admins and make admins (managing.py); one's own account keeps all
    three, and so does the last active superuser.
    """
    if target.pk is None or actor is None:
        return ""
    if reason := managing.manager_refusal(actor, target):
        return reason
    flags = ("is_active", "is_staff", "is_superuser")
    new = {name: bool(values.get(name, getattr(target, name))) for name in flags}
    lost = [name for name in flags if getattr(target, name) and not new[name]]
    gained = [name for name in ("is_staff", "is_superuser") if new[name] and not getattr(target, name)]
    if gained and not actor.is_superuser:
        return _("Only superusers can make a user an admin.")
    if lost and target.pk == actor.pk:
        return _("You can't disable your own account or take away your own admin role.")
    if lost and managing.is_full_admin(target) and not User.objects.filter(managing.ADMINS).exclude(pk=target.pk).exists():
        return _("This is the last working superuser: it can't be disabled or lose its admin role.")
    return ""


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = HFileUserChangeForm
    add_form = HFileUserCreationForm
    list_display = ["username", "nickname", "kind", "is_staff", "is_active", "last_login", "last_login_ip", "date_joined"]
    list_filter = ["kind", "is_staff", "is_superuser", "is_active"]
    search_fields = ["username", "nickname", "email"]
    ordering = ["id"]
    readonly_fields = ["kind", "last_login", "last_login_ip", "date_joined"]
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (gettext_lazy("Profile"), {"fields": ("nickname", "email")}),
        (gettext_lazy("Permissions"), {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        (gettext_lazy("Log in"), {"fields": ("kind", "last_login", "last_login_ip", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("username", "usable_password", "password1", "password2")}),
    )

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.current_user = request.user  # a class made for this request (modelform_factory)
        return form

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.is_builtin:
            # They never log in: their names stay, and they get no admin rights.
            fields += ["username", "nickname", "is_staff", "is_superuser", "groups", "user_permissions"]
        return fields

    def save_model(self, request, obj, form, change):
        if change:
            # Checked again on the rows as they are now, locked: two admins at the same moment can't
            # both demote one of the last two superusers (the form was checked before the locks).
            current, _admins = managing._lock(obj.pk)
            problem = admin_change_problem(request.user, current, {
                "is_active": obj.is_active, "is_staff": obj.is_staff, "is_superuser": obj.is_superuser,
            })
            if problem:
                raise PermissionDenied(problem)
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        if obj is not None and managing.manager_refusal(request.user, obj):
            return False  # another admin, for an admin who isn't a superuser: shown read-only
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and managing.rights(request.user, obj)["delete"]:
            return False  # built-ins, one's own account, the last active superuser, other admins
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        """The bulk action skips built-ins, one's own account and admins one may not change, and
        never deletes every active superuser."""
        allowed = [
            user.pk for user in queryset.filter(kind="normal").exclude(pk=request.user.pk)
            if not managing.manager_refusal(request.user, user)
        ]
        if not User.objects.filter(managing.ADMINS).exclude(pk__in=allowed).exists():
            self.message_user(request, _("Not every working superuser can be deleted; nothing was deleted."), messages.ERROR)
            return
        super().delete_queryset(request, queryset.filter(pk__in=allowed))

    def user_change_password(self, request, id, form_url=""):
        user = self.get_object(request, unquote(id))
        if user is not None and user.is_builtin:
            raise PermissionDenied(_("Built-in users have no password and can't log in."))
        return super().user_change_password(request, id, form_url)
