"""What the admin's actions on storage sources do: save (with its connection test), enable and disable,
copy, delete, sort, and the form's 测试连接, helper buttons and OAuth.

The connection test runs before anything is written: a candidate driver instance gets the new params
and checks them (apps/storages/instances.py); only when that works is the row saved, and the candidate then
replaces the live instance. A disabled storage is saved without a test and has no instance; enabling it
tests it first (decision D36).
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.urls import reverse
from django.utils.translation import gettext

from . import config as storage_config
from . import drafts, grants, instances
from .drivers.base import DriverError, Unsupported
from .models import FilterRule, FolderPassword, ReadmeRule, StorageSource, WhenFull, new_link_secret
from .serializers import StorageFieldsSerializer

logger = logging.getLogger("hfile.storages")

CHECK_FAILED = "check_failed"
SUPERUSER_REQUIRED = "superuser_required"

# Decision D50: a local storage reaches any folder the program's system account can read and write, so
# creating one, changing its folder and letting it follow links out of the folder are for superusers.
# Other admins may still edit, enable, copy (same folder) and delete local storages.
SUPERUSER_PARAMS: dict[str, tuple[str, ...]] = {"local": ("root", "allow_external_symlinks")}


class ActionError(Exception):
    """The action can't be done: a message for the form (and optionally field errors and changes).
    `status`: the HTTP status when it isn't the usual one for `code` (views._error)."""

    def __init__(self, message: str = "", *, errors: dict | None = None, code: str = "", extra: dict | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or {}
        self.code = code
        self.extra = extra or {}
        self.status = status

    def data(self) -> dict[str, Any]:
        if self.errors:
            return {**self.errors, **self.extra}
        answer: dict[str, Any] = {"detail": self.message}
        if self.code:
            answer["code"] = self.code
        answer.update(self.extra)
        return answer


def superuser_refusal(user, type_: str, stored: dict | None, submitted: dict) -> str:
    """Why `user` may not save or try the form's params `submitted` (as sent) for a storage of `type_`
    whose saved params are `stored` (None: a storage not saved yet); "" when he may (D50).

    Decided on what was sent, before anything looks at the disk (the local driver's checks say whether a
    folder exists). A param left out stays as saved; any other spelling of the folder counts as another
    folder; a switch counts when it is turned on."""
    if type_ not in SUPERUSER_PARAMS or getattr(user, "is_superuser", False):
        return ""
    if stored is None:
        return gettext("Only superusers can create local storage sources: one can put any folder the program can read and write on the server onto the site.")
    params = storage_config.params(storage_config.driver(type_))
    for key in SUPERUSER_PARAMS[type_]:
        if key not in submitted:
            continue
        param, raw = params[key], submitted[key]
        if param.kind == "bool":
            try:
                on = storage_config.check_value(param, raw)
            except ValueError:
                continue  # refused by the form's checks before anything is used
            if on and not stored.get(key):
                return gettext('Only superusers can turn on "%(label)s".') % {"label": param.label}
        elif raw != stored.get(key):
            return gettext('Only superusers can change "%(label)s" of a local storage source.') % {"label": param.label}
    return ""


def _superuser_only(request, storage: StorageSource | None, type_: str, cls, submitted: dict) -> None:
    if type_ not in SUPERUSER_PARAMS:
        return
    stored = storage_config.decode(cls, storage.config)[0] if storage else None
    reason = superuser_refusal(request.user, type_, stored, submitted)
    if reason:
        raise ActionError(reason, code=SUPERUSER_REQUIRED, status=403)


def unusable_type(error: storage_config.ConfigError) -> str:
    """The message of a saved storage whose type can't be used now (STOR-01)."""
    return gettext("This storage source's type can't be used now: %(problems)s") % {"problems": gettext("; ").join(map(str, error.errors.values()))}


def _start(make, *args) -> instances.Handle:
    """instances.candidate() or temporary(), whose values may not make a driver: a saved storage's type is
    gone or its library missing, a value was emptied in the database by hand (STOR-01). ActionError,
    not a 500."""
    try:
        return make(*args)
    except storage_config.ConfigError as error:
        raise ActionError(unusable_type(error), code="type_unavailable") from None
    except DriverError as error:
        raise ActionError(error.message) from None
    except Exception:  # noqa: BLE001 - a driver's bug with these values: the traceback goes to the log
        logger.exception("A %s driver can't start with these settings", args[-2] if len(args) > 1 else "")
        raise ActionError(gettext("The storage driver can't start with these settings. Please check them; the details are in the program's log.")) from None


def _check(handle: instances.Handle) -> str:
    """Runs the driver's check(); "" when it works, else the message for the admin."""
    try:
        handle.driver.check()
    except DriverError as error:
        return error.message
    except Exception:  # noqa: BLE001 - a driver's bug: the traceback goes to the log, the form gets a hint
        logger.exception("Checking a %s storage failed", handle.type)
        return gettext("The program failed while checking the connection; the details are in the program's log.")
    return ""


def site_origin(request) -> str:
    """scheme://host[:port] of the site: [site] url, else the address this request came to."""
    if settings.SITE_URL:
        from urllib.parse import urlsplit

        split = urlsplit(settings.SITE_URL)
        return f"{split.scheme}://{split.netloc}"
    return f"{request.scheme}://{request.get_host()}"


def callback_url(request) -> str:
    """The OAuth redirect URI: the site's own /storages/oauth/callback/ (decision D14)."""
    if settings.SITE_URL:
        return settings.SITE_URL.rstrip("/") + reverse("storages:oauth-callback")
    return site_origin(request) + reverse("storages:oauth-callback")


def _config_input(data: dict) -> tuple[dict, set[str]]:
    config = data.get("config", {})
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ActionError(errors={"config": [gettext("The settings should be an object.")]})
    clear = data.get("clear_secrets") or []
    if not isinstance(clear, list) or not all(isinstance(key, str) for key in clear):
        raise ActionError(errors={"clear_secrets": [gettext("Should be a list of setting names.")]})
    return config, set(clear)


def _clean(cls, submitted: dict, *, stored: dict, draft: dict | None, clear: set[str], partial: bool,
           later: set[str] = frozenset()) -> dict:
    try:
        return storage_config.clean(cls, submitted, stored=stored, draft=drafts.secret_values(draft, cls),
                                    clear=clear, partial=partial, later=later)
    except storage_config.ConfigError as error:
        raise ActionError(errors={"config": {key: [message] for key, message in error.errors.items()}}) from None


def _fields(data: dict, storage: StorageSource | None) -> dict:
    serializer = StorageFieldsSerializer(data=data, storage=storage, partial=storage is not None)
    if not serializer.is_valid():
        raise ActionError(errors=dict(serializer.errors))
    fields = dict(serializer.validated_data)
    type_ = fields.get("type") or (storage.type if storage else "")
    cls = storage_config.driver(type_)
    # Options of a kind the driver can't use mean nothing: off.
    if "proxy_download" in fields and not cls.can_direct_download:
        fields["proxy_download"] = False
    if "proxy_when_full" in fields and not cls.can_direct_download:  # no link of its own to send to
        fields["proxy_when_full"] = WhenFull.REFUSE
    if "proxy_upload" in fields and not cls.can_direct_upload:
        fields["proxy_upload"] = False
    return fields


SITE_FIELDS = ("key", "name", "enabled", "remark", "download_domain", "proxy_download", "proxy_connections",
               "proxy_when_full", "proxy_upload", "force_download", "link_lifetime", "permanent_links",
               "gallery_default", "readme_from_file")


def save(request, storage: StorageSource | None, data: dict) -> tuple[StorageSource, dict[str, Any]]:
    """Create (storage None) or change a storage from the form's data. Returns (row, extra) where extra
    says how many users got a grant (a new storage). Raises ActionError."""
    if not isinstance(data, dict):
        raise ActionError(gettext("The request is malformed."))
    fields = _fields(data, storage)
    type_ = storage.type if storage else fields["type"]
    cls = storage_config.driver(type_)
    # The add wizard saves its second step before 授权 ("setup"): the new storage stays off, and the
    # required params its later steps fill (a SharePoint site, chosen after 授权) may still be empty.
    # The same while it goes on with the storage it saved, still off: 保存并继续, and 授权's tokens.
    setup = data.get("setup") is True and (storage is None or not storage.enabled)
    later: set[str] = set()
    if setup:
        fields["enabled"] = False
        later = set(storage_config.params(cls)) - set(storage_config.setup_keys(cls))
    submitted, clear = _config_input(data)
    _superuser_only(request, storage, type_, cls, submitted)
    draft = drafts.get(request, data.get("draft"), storage_id=storage.pk if storage else None, type_=type_)
    stored, _unreadable = storage_config.decode(cls, storage.config) if storage else ({}, [])
    params_sent = storage is None or "config" in data or bool(clear) or bool(draft and draft.get("secrets"))
    if params_sent:
        cleaned = _clean(cls, submitted, stored=stored, draft=draft, clear=clear, partial=storage is not None,
                         later=later)
    else:
        cleaned = dict(stored)
    config_changed = storage is None or (params_sent and (
        cleaned != {key: stored.get(key) for key in cleaned} or bool(_unreadable)))
    enabled = fields.get("enabled", storage.enabled if storage else True)
    needs_check = enabled and (config_changed or not storage.enabled)

    handle = None
    if needs_check:
        handle = _start(instances.candidate, storage.pk if storage else None,
                        storage.config_revision if storage else 0, type_, cleaned)
        problem = _check(handle)
        if problem:
            kept = _keep_changes(request, handle, draft, storage, type_, cls)
            handle.close()
            raise ActionError(gettext("Connection failed: %(problem)s") % {"problem": problem}, code=CHECK_FAILED, extra={"draft": kept} if kept else None)
    changes, _before = handle.take_changes() if handle else ({}, {})
    merged = {**cleaned, **changes}
    extra: dict[str, Any] = {}
    try:
        with transaction.atomic():
            if storage is None:
                position = (StorageSource.objects.aggregate(top=Max("position"))["top"] or 0) + 1
                row = StorageSource(type=type_, position=position,
                                    config=storage_config.encode(cls, merged), config_revision=1)
                for name in SITE_FIELDS:
                    if name in fields:
                        setattr(row, name, fields[name])
                row.save()
                extra["grants"] = grants.grant_new_storage(row)
            else:
                row = StorageSource.objects.select_for_update().get(pk=storage.pk)
                for name in SITE_FIELDS:
                    if name in fields:
                        setattr(row, name, fields[name])
                if config_changed:
                    current, _ = storage_config.decode(cls, row.config)
                    sent = {key for key, value in submitted.items() if value not in (None, "")}
                    kept = drafts.secret_values(draft, cls)
                    for key in storage_config.secret_keys(cls):
                        # A secret the form kept: the stored value as it is now (a driver may have
                        # replaced a token since the form was opened).
                        if key not in sent and key not in kept and key not in changes and key not in clear and key in current:
                            merged[key] = current[key]
                    row.config = storage_config.encode(cls, merged)
                    row.config_revision += 1
                row.save()
    except Exception as error:
        if handle is not None:
            handle.put_back(changes, _before)  # a token the test refreshed must not be lost with the save
            kept = _keep_changes(request, handle, draft, storage, type_, cls)
            handle.close()
        else:
            kept = None
        if isinstance(error, IntegrityError):  # another admin took the key a moment ago
            raise ActionError(errors={"key": [gettext("Another storage source has this alias. Please choose another.")]},
                              extra={"draft": kept} if kept else None) from None
        raise
    if handle is not None:
        handle.storage_id = row.pk
        handle.revision = row.config_revision
        instances.publish(handle)
        instances.record(row.pk, True, gettext("The connection works."))
    elif not row.enabled or config_changed:
        instances.drop(row.pk)
    if draft is not None:
        drafts.drop(request, draft["id"])
    return row, extra


def _keep_changes(request, handle, draft, storage, type_, cls) -> str | None:
    """A candidate or temporary instance changed secrets (a refreshed token) but nothing was saved:
    the stored ones it replaced are saved now (instances.apply_temporary), the rest go to the draft,
    whose id is returned (the form must send it with its next save)."""
    rest = instances.apply_temporary(handle)
    if not rest:
        return None
    draft = draft or drafts.ensure(request, None, storage_id=storage.pk if storage else None, type_=type_)
    drafts.put(draft, cls, rest)
    drafts.save(request, draft)
    return draft["id"]


def set_enabled(storage: StorageSource, enabled: bool) -> StorageSource:
    """Enable (after a connection test) or disable a storage. Raises ActionError."""
    if not enabled:
        StorageSource.objects.filter(pk=storage.pk).update(enabled=False)
        instances.drop(storage.pk)
        storage.enabled = False
        return storage
    if storage.enabled:
        return storage
    cls = storage_config.driver(storage.type)
    plain, _unreadable = storage_config.decode(cls, storage.config)
    handle = _start(instances.candidate, storage.pk, storage.config_revision, storage.type, plain)
    problem = _check(handle)
    if problem:
        instances.apply_temporary(handle)
        handle.close()
        instances.record(storage.pk, False, problem)
        raise ActionError(gettext("Enabling failed; it couldn't connect: %(problem)s") % {"problem": problem}, code=CHECK_FAILED)
    StorageSource.objects.filter(pk=storage.pk).update(enabled=True)
    storage.enabled = True
    instances.publish(handle)
    instances.record(storage.pk, True, gettext("The connection works."))
    return storage


def recheck(storage: StorageSource) -> dict[str, Any]:
    """重试: check the saved params; the live instance does it for an enabled storage."""
    if storage.enabled:
        try:
            instances.get(storage).check()
            problem = ""
        except instances.StorageUnavailable as error:
            problem = error.message
        except DriverError as error:
            problem = error.message
        except Exception:  # noqa: BLE001
            logger.exception("Checking storage %s failed", storage.pk)
            problem = gettext("The program failed while checking the connection; the details are in the program's log.")
    else:
        cls = storage_config.driver(storage.type)
        plain, _unreadable = storage_config.decode(cls, storage.config)
        handle = _start(instances.temporary, storage.pk, storage.type, plain)
        try:
            problem = _check(handle)
        finally:
            instances.apply_temporary(handle)
            handle.close()
    instances.record(storage.pk, not problem, problem or gettext("The connection works."))
    return instances.status(storage.pk)


def delete(storage: StorageSource) -> None:
    """Deletes the storage with its grants and rules (and, through their foreign keys, its links, shares
    and logs: decision D6)."""
    with transaction.atomic():
        StorageSource.objects.filter(pk=storage.pk).delete()
    instances.forget(storage.pk)


# Tokens that a storage can't share with a copy of it: 115 replaces its refresh token on every refresh and
# the old one stops working, so two storages with the same one would lock each other out. A copy starts
# without them (and disabled): the admin logs in again on it.
UNSHARED_SECRETS = {"open115": ("access_token", "refresh_token", "token_expires_at")}


def copy(storage: StorageSource, *, key: str, name: str) -> StorageSource:
    """A new storage with the same type, params, options, rules and grants."""
    data = {"key": key, "name": name, "type": storage.type}
    serializer = StorageFieldsSerializer(data=data, partial=True)
    if not serializer.is_valid():
        errors = {k: v for k, v in serializer.errors.items() if k in ("key", "name")}
        if not errors:  # the type's: gone (STOR-01); its message, not an empty one
            raise ActionError(gettext("This storage source's type can't be used now, so it can't be copied: %(problems)s") % {"problems": gettext("; ").join(
                str(message) for messages in serializer.errors.values() for message in messages)}, code="type_unavailable")
        raise ActionError(errors=errors)
    with transaction.atomic():
        source = StorageSource.objects.select_for_update().get(pk=storage.pk)
        position = (StorageSource.objects.aggregate(top=Max("position"))["top"] or 0) + 1
        config = dict(source.config or {})
        unshared = UNSHARED_SECRETS.get(source.type, ())
        for param in unshared:
            config[param] = ""
        row = StorageSource.objects.create(
            key=serializer.validated_data["key"], name=serializer.validated_data["name"], type=source.type,
            enabled=source.enabled and not unshared, position=position, remark=source.remark, config=config,
            download_domain=source.download_domain, proxy_download=source.proxy_download,
            proxy_connections=source.proxy_connections, proxy_when_full=source.proxy_when_full,
            proxy_upload=source.proxy_upload, force_download=source.force_download,
            link_lifetime=source.link_lifetime, permanent_links=source.permanent_links,
            link_secret=new_link_secret(), gallery_default=source.gallery_default,
            readme_from_file=source.readme_from_file,
        )
        FilterRule.objects.bulk_create([
            FilterRule(storage=row, position=rule.position, expression=rule.expression,
                       description=rule.description, mode=rule.mode)
            for rule in source.filter_rules.all()
        ])
        FolderPassword.objects.bulk_create([
            FolderPassword(storage=row, position=rule.position, expression=rule.expression,
                           description=rule.description, password=rule.password)
            for rule in source.folder_passwords.all()
        ])
        ReadmeRule.objects.bulk_create([
            ReadmeRule(storage=row, position=rule.position, expression=rule.expression,
                       description=rule.description, text=rule.text, path_mode=rule.path_mode,
                       display=rule.display)
            for rule in source.readme_rules.all()
        ])
        grants.copy_storage_grants(source, row)
    return row


def sort(ids: list[int]) -> None:
    """Puts the storages in the order of `ids`, which must name each storage once. Raises ActionError."""
    if not isinstance(ids, list) or not all(type(pk) is int for pk in ids):
        raise ActionError(errors={"ids": [gettext("Should be a list of storage source ids.")]})
    with transaction.atomic():
        current = {row.pk: row.position for row in StorageSource.objects.select_for_update().only("position")}
        if sorted(ids) != sorted(current) or len(ids) != len(set(ids)):
            raise ActionError(gettext("Storage sources were added or removed. Please refresh the list and sort again."), code="conflict")
        for position, pk in enumerate(ids, start=1):
            if current[pk] != position:
                StorageSource.objects.filter(pk=pk).update(position=position)


def rotate_link_secret(storage: StorageSource) -> None:
    """换一个链接密钥: every permanent link of the storage stops working."""
    StorageSource.objects.filter(pk=storage.pk).update(link_secret=new_link_secret())


# --- the form's buttons --------------------------------------------------------------------------------

def _form_context(request, data: dict) -> tuple[StorageSource | None, str, type, dict]:
    """(storage or None, type, driver class, draft) of a request from the form."""
    if not isinstance(data, dict):
        raise ActionError(gettext("The request is malformed."))
    storage = None
    storage_id = data.get("storage")
    if storage_id is not None:
        if type(storage_id) is not int:
            raise ActionError(errors={"storage": [gettext("Should be a storage source id.")]})
        storage = StorageSource.objects.filter(pk=storage_id).first()
        if storage is None:
            raise ActionError(gettext("This storage source no longer exists."), code="not_found")
        type_ = storage.type
    else:
        type_ = data.get("type")
        if not isinstance(type_, str) or not storage_config.known_type(type_):
            raise ActionError(errors={"type": [gettext("Unknown storage type.")]})
    cls = storage_config.driver(type_)
    draft = drafts.ensure(request, data.get("draft"), storage_id=storage.pk if storage else None, type_=type_)
    return storage, type_, cls, draft


def _loose_config(cls, storage: StorageSource | None, draft: dict, submitted: dict) -> dict:
    """The form's values as far as they are filled in (helpers and OAuth run on unfinished forms):
    each typed value checked, secrets from the draft or the stored config, no required fields."""
    stored, _ = storage_config.decode(cls, storage.config) if storage else ({}, [])
    kept = drafts.secret_values(draft, cls)
    errors: dict[str, list[str]] = {}
    config: dict[str, Any] = {}
    for key, param in storage_config.params(cls).items():
        raw = submitted.get(key)
        try:
            value = None if raw in (None, "") else storage_config.check_value(param, raw)
        except ValueError as error:
            errors[key] = [str(error)]
            continue
        if value in (None, ""):
            if param.secret:
                value = kept.get(key, stored.get(key))
            elif key not in submitted:
                value = stored.get(key, param.default)
            else:
                value = param.default
        config[key] = value
    unknown = [key for key in submitted if key not in storage_config.params(cls)]
    for key in unknown:
        errors[key] = [gettext("This kind of storage has no such setting.")]
    if errors:
        raise ActionError(errors={"config": errors})
    return config


def test_form(request, data: dict) -> dict[str, Any]:
    """测试连接 with the form as it is, saving nothing (decision D36)."""
    storage, type_, cls, draft = _form_context(request, data)
    submitted, clear = _config_input(data)
    _superuser_only(request, storage, type_, cls, submitted)
    stored, _ = storage_config.decode(cls, storage.config) if storage else ({}, [])
    cleaned = _clean(cls, submitted, stored=stored, draft=draft, clear=clear, partial=False)
    handle = _start(instances.temporary, storage.pk if storage else None, type_, cleaned)
    try:
        problem = _check(handle)
    finally:
        rest = instances.apply_temporary(handle)
        handle.close()
    secret_keys, changes = drafts.put(draft, cls, rest)
    drafts.save(request, draft)
    return {"ok": not problem, "message": problem or gettext("Connected."), "changes": changes, "secrets": secret_keys,
            "draft": draft["id"]}


def _choices(answer_choices) -> list[dict[str, str]]:
    choices = []
    for item in answer_choices if isinstance(answer_choices, list) else []:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            choices.append({"value": item["value"], "label": str(item.get("label") or item["value"])})
    return choices


def run_helper(request, data: dict) -> dict[str, Any]:
    """One of the driver's helper buttons, with the form's values; admins only (it makes the server
    connect to what the form says). A poll's arguments stay on the server, and so do secrets."""
    storage, type_, cls, draft = _form_context(request, data)
    name = data.get("name")
    if not isinstance(name, str) or name not in cls.helpers:
        raise ActionError(errors={"name": [gettext("This kind of storage has no such button.")]})
    submitted, _clear = _config_input(data)
    _superuser_only(request, storage, type_, cls, submitted)
    config = _loose_config(cls, storage, draft, submitted)
    args: dict[str, Any] = {}
    poll = data.get("poll_token")
    if poll:
        args = drafts.take_poll(draft, poll)
        if args is None:
            drafts.save(request, draft)
            raise ActionError(gettext("The scan login has expired. Please click the button again."), code="poll_expired")
    args["origin"] = site_origin(request)
    try:
        answer = cls.helper(name, config, args)
    except Unsupported:
        raise ActionError(gettext("This kind of storage doesn't support this button.")) from None
    except DriverError as error:
        drafts.save(request, draft)
        raise ActionError(error.message) from None
    if not isinstance(answer, dict):
        answer = {}
    result: dict[str, Any] = {"draft": draft["id"]}
    if isinstance(answer.get("message"), str):
        result["message"] = answer["message"]
    if isinstance(answer.get("param"), str) and answer["param"] in storage_config.params(cls):
        result["param"] = answer["param"]
        result["choices"] = _choices(answer.get("choices"))
    if isinstance(answer.get("image"), str) and answer["image"].startswith("data:image/"):
        result["image"] = answer["image"]
    if isinstance(answer.get("poll"), str) and answer["poll"] in cls.helpers and isinstance(answer.get("args"), dict):
        result["poll"] = answer["poll"]
        result["poll_token"] = drafts.put_poll(draft, answer["args"])
    if isinstance(answer.get("changes"), dict):
        secret_keys, changes = drafts.put(draft, cls, answer["changes"])
        result["changes"] = changes
        result["secrets"] = secret_keys
    drafts.save(request, draft)
    return result


def start_oauth(request, data: dict) -> dict[str, Any]:
    """授权: the provider's consent page for the form's app; the callback (views.oauth_callback) puts
    the tokens into the form's draft."""
    storage, type_, cls, draft = _form_context(request, data)
    if not cls.oauth:
        raise ActionError(gettext("This kind of storage needs no authorization."))
    submitted, _clear = _config_input(data)
    _superuser_only(request, storage, type_, cls, submitted)
    config = _loose_config(cls, storage, draft, submitted)
    drafts.set_form(draft, cls, data.get("form"))
    redirect_uri = callback_url(request)
    # From the add wizard (a saved storage): the callback's way back is the wizard, which saves the tokens.
    setup = storage is not None and data.get("setup") is True
    state = drafts.start_oauth(request, draft=draft, cls=cls, config=config, redirect_uri=redirect_uri, setup=setup)
    try:
        url = cls.authorize_url(config, redirect_uri=redirect_uri, state=state)
    except DriverError as error:
        drafts.take_oauth(request, state)
        drafts.save(request, draft)
        raise ActionError(error.message) from None
    drafts.save(request, draft)
    return {"url": url, "redirect_uri": redirect_uri, "draft": draft["id"]}


def finish_oauth(request, flow: dict, code: str) -> dict[str, Any]:
    """The callback's half: exchange the code and keep the tokens in the form's draft. Raises DriverError."""
    cls = storage_config.driver(flow["type"])
    config, _ = storage_config.decode(cls, flow.get("config") or {})
    params = cls.exchange_code(config, code=code, redirect_uri=flow["redirect_uri"])
    known = {key: value for key, value in dict(params or {}).items() if key in storage_config.params(cls)}
    draft = drafts.ensure(request, flow["draft"], storage_id=flow.get("storage"), type_=flow["type"])
    secret_keys, _changes = drafts.put(draft, cls, known)
    drafts.save(request, draft)
    return {"draft": draft, "secrets": secret_keys}
