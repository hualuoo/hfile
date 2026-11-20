"""User management on /manage/ (apps/accounts/manage_views.py): who may change whom, and the changes.

Only admins get here (apps.core.permissions.IsAdmin: active staff). Then:

- The built-in users (the guest and the template, models.Kind) keep their names, never get a password
  and can't be deleted. The template never logs in, so it has no 启用 switch; the guest's turns off
  what visitors who aren't logged in may do.
- Nobody disables or deletes their own account here, nor sets their own password: the user menu's
  修改密码 asks for the old one.
- A superuser's account, or another admin's, is changed (or copied) only by a superuser: an admin who
  isn't one could otherwise take it over by giving it a new password.
- There is always an active superuser: the last one can't be disabled or deleted, here or in the
  Django admin, nor lose is_staff or is_superuser there (apps/accounts/admin.py).
- Nobody is made an admin here: superusers come from the installer, `createsuperuser` and the Django
  admin.

Each change locks the rows it depends on (the user and the active superusers, in id order) and checks
again on what is there now, so two admins changing users at the same moment can't both pass a check
that only one of them should. Each change is logged in the activity log in the same transaction.
"""

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils.translation import gettext as _, gettext_lazy
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.activity import log as activity

from .models import Kind, User
from .permissions import usable
from .signals import user_copied

# The admins who can do everything: what must always exist.
ADMINS = Q(kind=Kind.NORMAL, is_active=True, is_staff=True, is_superuser=True)

ONLY_SUPERUSERS = gettext_lazy("Only superusers can change other admins' accounts.")
BUILTIN_NAME = gettext_lazy("A built-in user's username and nickname can't be changed.")
BUILTIN_PASSWORD = gettext_lazy("Built-in users have no password and can't log in.")
BUILTIN_DELETE = gettext_lazy("Built-in users can't be deleted.")
TEMPLATE_SWITCH = gettext_lazy("The new user template doesn't log in, so it isn't enabled or disabled.")
OWN_PASSWORD = gettext_lazy('To change your own password, use "Change password" in the menu at the top right.')
OWN_DISABLE = gettext_lazy("You can't disable your own account.")
OWN_DELETE = gettext_lazy("You can't delete your own account.")
LAST_DISABLE = gettext_lazy("This is the last working superuser; it can't be disabled.")
LAST_DELETE = gettext_lazy("This is the last working superuser; it can't be deleted.")
USERNAME_TAKEN = gettext_lazy("This username is taken.")


def is_full_admin(user):
    """An active superuser that is staff: one of ADMINS."""
    return user.kind == Kind.NORMAL and user.is_active and user.is_staff and user.is_superuser


def admin_count():
    return User.objects.filter(ADMINS).count()


def manager_refusal(actor, target):
    """Why `actor` may not change `target` at all, or ""."""
    if target.pk != actor.pk and (target.is_superuser or target.is_staff) and not actor.is_superuser:
        return ONLY_SUPERUSERS
    return ""


def _first(*reasons):
    return next((reason for reason in reasons if reason), "")


def rights(actor, target, admins=None):
    """What the page may offer `actor` for `target`: {action: why not, or ""}. `admins` is how many
    active superusers there are (admin_count(), counted once for a whole list).

    toggle is the 启用 switch: turning it off when the user is active, on when not.
    """
    if admins is None:
        admins = admin_count()
    own = target.pk == actor.pk
    last = is_full_admin(target) and admins <= 1
    base = manager_refusal(actor, target)
    if target.kind == Kind.TEMPLATE:
        toggle = _first(base, TEMPLATE_SWITCH)
    elif target.is_active:
        toggle = _first(base, OWN_DISABLE if own else "", LAST_DISABLE if last else "")
    else:
        toggle = base
    return {
        "edit": base,
        "rename": _first(base, BUILTIN_NAME if target.is_builtin else ""),
        "password": _first(base, BUILTIN_PASSWORD if target.is_builtin else "", OWN_PASSWORD if own else ""),
        "toggle": toggle,
        "delete": _first(base, BUILTIN_DELETE if target.is_builtin else "", OWN_DELETE if own else "",
                         LAST_DELETE if last else ""),
        "copy": base,
    }


def _lock(target_id):
    """The user `target_id` and the ids of the active superusers, their rows locked (in id order, so
    that two of these never wait for each other). NotFound when the user is gone."""
    rows = list(User.objects.select_for_update().filter(Q(pk=target_id) | ADMINS).order_by("pk"))
    target = next((user for user in rows if user.pk == target_id), None)
    if target is None:
        raise NotFound(_("This user doesn't exist any more; it may have just been deleted."))
    return target, {user.pk for user in rows if is_full_admin(user)}


def _save(user):
    """Saves a new or changed user; the unique index has the last word on the username."""
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError:
        if User.objects.username_taken(user.username, exclude=user.pk):
            raise ValidationError({"username": [USERNAME_TAKEN]}) from None
        raise


def create_user(actor, data):
    """A new user of the normal kind from the form's checked `data`. No password (or an empty one)
    is an unusable one: the account can't log in with a password until it gets one."""
    user = User(
        username=data["username"],
        nickname=data.get("nickname", ""),
        is_active=data.get("is_active", True),
        default_permissions=data.get("default_permissions", []),
    )
    user.password = make_password(data.get("password") or None)
    detail = {}
    if not user.is_active:
        detail["enabled"] = False
    if not data.get("password"):
        detail["password"] = False
    with transaction.atomic():
        _save(user)
        activity.record(activity.Kind.CREATE_USER, actor=actor, target=user, detail=detail)
    return user


def _changes(target, data):
    """What `data` changes on `target`, for the activity log: [old, new] of the names, "enabled", the
    permissions added and removed, "password": True; never the password itself."""
    changes = {}
    for name in ("username", "nickname"):
        if name in data and data[name] != getattr(target, name):
            changes[name] = [getattr(target, name), data[name]]
    if "is_active" in data and data["is_active"] != target.is_active:
        changes["enabled"] = data["is_active"]
    if "default_permissions" in data:
        old, new = list(target.default_permissions or []), list(data["default_permissions"])
        added, removed = [code for code in new if code not in old], [code for code in old if code not in new]
        if added or removed:
            changes["permissions"] = {"added": added, "removed": removed}
    if data.get("password"):
        changes["password"] = True
    return changes


def update_user(actor, target_id, data):
    """Changes what the form sent (`data`, checked by UserFormSerializer; a password "" or left out
    keeps the old one). Raises PermissionDenied, or ValidationError naming the fields."""
    with transaction.atomic():
        target, admins = _lock(target_id)
        if reason := manager_refusal(actor, target):
            raise PermissionDenied(reason)
        own = target.pk == actor.pk
        errors = {}
        if target.is_builtin:
            for name in ("username", "nickname"):
                if name in data and data[name] != getattr(target, name):
                    errors[name] = [BUILTIN_NAME]
            if data.get("password"):
                errors["password"] = [BUILTIN_PASSWORD]
        elif data.get("password") and own:
            errors["password"] = [OWN_PASSWORD]
        if "is_active" in data and data["is_active"] != target.is_active:
            if target.kind == Kind.TEMPLATE:
                errors["is_active"] = [TEMPLATE_SWITCH]
            elif not data["is_active"] and own:
                errors["is_active"] = [OWN_DISABLE]
            elif not data["is_active"] and is_full_admin(target) and not admins - {target.pk}:
                errors["is_active"] = [LAST_DISABLE]
        if errors:
            raise ValidationError(errors)
        changes = _changes(target, data)
        for name in ("username", "nickname", "is_active", "default_permissions"):
            if name in data:
                setattr(target, name, data[name])
        if data.get("password"):
            # A new password ends the sessions the user has (the session hash is made from it).
            target.set_password(data["password"])
        _save(target)
        if changes:
            activity.record(activity.Kind.UPDATE_USER, actor=actor, target=target, detail=changes)
    return target


def copy_user(actor, source_id, data):
    """A new user like `source_id`: its 启用, its default permissions and (through the user_copied
    signal, which the storages app answers) its grants on every storage source. Not its password
    (copying the hash would give two accounts one password), nor admin rights or two-step
    verification. data: username, and maybe nickname and password (none: an unusable one)."""
    with transaction.atomic():
        source = User.objects.filter(pk=source_id).first()
        if source is None:
            raise NotFound(_("This user doesn't exist any more; it may have just been deleted."))
        if reason := manager_refusal(actor, source):
            raise PermissionDenied(reason)
        target = User(
            username=data["username"],
            nickname=data.get("nickname", ""),
            # The built-ins' switch means something else (visitors' access): a copy starts enabled.
            is_active=source.is_active if source.kind == Kind.NORMAL else True,
            default_permissions=usable(source.default_permissions or []),  # without what misses a prerequisite (D55)
        )
        target.password = make_password(data.get("password") or None)
        _save(target)
        user_copied.send(sender=User, source=source, target=target)
        activity.record(activity.Kind.COPY_USER, actor=actor, target=target, detail={"from": source.username})
    return target


def delete_user(actor, target_id):
    """Deletes a user and what is theirs (their grants and shares, through the foreign keys). Their
    entries in the activity log stay, with the username. Raises PermissionDenied with the reason."""
    with transaction.atomic():
        target, admins = _lock(target_id)
        reason = rights(actor, target, admins=len(admins))["delete"]
        if reason:
            raise PermissionDenied(reason)
        activity.record(activity.Kind.DELETE_USER, actor=actor, target=target)
        target.delete()
