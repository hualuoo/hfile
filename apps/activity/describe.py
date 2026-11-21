"""What an entry of the 操作日志 says beyond its kind, in the language of the page reading it.

Entries keep codes and plain values in `detail` (apps/activity/log.py), never text in one language: the
labels come from where they are defined (the permissions, the settings' fields, the storage types and their
params), as they read now. describe(entry) gives the text of the page's 详情 column ("" when the kind and
what the entry is about say it all).
"""

from functools import lru_cache

from django.template.defaultfilters import filesizeformat
from django.utils import formats, timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import capfirst
from django.utils.translation import gettext as _, ngettext, pgettext

from .models import ActivityLog

Kind = ActivityLog.Kind

SHOWN_NAMES = 3  # of a list, before "… (n in all)"


def _join(parts):
    return _(", ").join(str(part) for part in parts if part)


def _sentences(parts):
    return _("; ").join(str(part) for part in parts if part)


def _parts(parts):
    """The parts of one change ("added …, removed …"): not a list of names, which _join makes."""
    return pgettext("between the parts of one change", ", ").join(str(part) for part in parts if part)


def _names(value):
    """A list kept as {"names", "count"} (log.some): the first few, and how many there were."""
    if not isinstance(value, dict):
        return ""
    names = [str(name) for name in value.get("names") or [] if name]
    count = value.get("count") if isinstance(value.get("count"), int) else len(names)
    shown = _join(names[:SHOWN_NAMES])
    if count > SHOWN_NAMES and shown:
        return ngettext("%(names)s, … (%(count)s in all)", "%(names)s, … (%(count)s in all)", count) % {
            "names": shown, "count": count}
    return shown


def _moment(value):
    moment = parse_datetime(value) if isinstance(value, str) else None
    if moment is None:
        return ""
    return formats.date_format(timezone.localtime(moment), "Y-m-d H:i")


def _day(value):
    """The day of a moment where the site is (its TIME_ZONE), as the pages show it."""
    moment = parse_datetime(value) if isinstance(value, str) else None
    if moment is None:
        return ""
    return formats.date_format(timezone.localtime(moment), "Y-m-d")


def _size(value):
    return filesizeformat(value) if isinstance(value, int) and value >= 0 else ""


def _expiry(value):
    if value is None:
        return _("Never expires")
    moment = _moment(value)
    return _("Expires %(time)s") % {"time": moment} if moment else ""


def _change(label, values):
    """「label: old → new」 for [old, new]."""
    if not isinstance(values, list) or len(values) != 2:
        return ""
    old, new = (str(value) if value else _("(empty)") for value in values)
    return _("%(label)s: %(old)s → %(new)s") % {"label": label, "old": old, "new": new}


def _permission_labels(codes):
    from apps.accounts.permissions import Permission

    labels = []
    for code in codes or []:
        try:
            labels.append(Permission(code).label)
        except ValueError:
            labels.append(code)
    return _join(labels)


def _added_removed(value):
    """{"added": [...], "removed": [...]} of permission codes."""
    if not isinstance(value, dict):
        return ""
    parts = []
    if value.get("added"):
        parts.append(_("added %(items)s") % {"items": _permission_labels(value["added"])})
    if value.get("removed"):
        parts.append(_("removed %(items)s") % {"items": _permission_labels(value["removed"])})
    return _parts(parts)


# ------------------------------------------------------------ labels defined elsewhere


@lru_cache(maxsize=None)
def _settings_labels(group):
    """{field: label} of a page of settings (apps/siteconf/serializers.py), lazy texts."""
    from apps.siteconf.serializers import GROUPS

    serializer = GROUPS.get(group)
    if serializer is None:
        return {}, ""
    return {name: field.label for name, field in serializer().fields.items()}, serializer.label


def _model_labels(model):
    return {field.name: field.verbose_name for field in model._meta.get_fields() if hasattr(field, "verbose_name")}


def _driver(type_):
    from apps.storages import config as storage_config

    try:
        return storage_config.driver(type_) if type_ else None
    except storage_config.ConfigError:
        return None


def _type_label(type_):
    cls = _driver(type_)
    return str(cls.label) if cls is not None else (type_ or "")


def _param_labels(type_, keys):
    cls = _driver(type_)
    labels = {param.key: param.label for param in cls.params} if cls is not None else {}
    return _join(labels.get(key, key) for key in keys or [])


# ------------------------------------------------------------ each kind


def _login(entry, detail):
    from apps.accounts.loginlog import SOURCES

    source = detail.get("source")
    return str(SOURCES[source]) if source in SOURCES and source != "site" else ""


def _login_failed(entry, detail):
    from apps.accounts.loginlog import REASONS

    reason = detail.get("reason") or ""
    # Single sign-on's reasons say where it was already.
    parts = [REASONS.get(reason, reason), "" if reason.startswith("sso_") else _login(entry, detail)]
    if detail.get("username"):
        parts.append(_('typed "%(username)s"') % {"username": detail["username"]})
    return _sentences(parts)


def _password(entry, detail):
    return _("Set a password (there was none)") if detail.get("first") else ""


def _provider(entry, detail):
    return detail.get("provider") or ""


def _identity(entry, detail):
    return " ".join(part for part in (detail.get("provider"), detail.get("external_id")) if part)


def _mkdir(entry, detail):
    return _("For a folder upload") if detail.get("upload") else ""


def _rename(entry, detail):
    return f"→ {entry.to_path.rstrip('/').rpartition('/')[2]}" if entry.to_path else ""


def _relocate(entry, detail):
    if not entry.to_path:
        return ""
    folder = entry.to_path.rstrip("/").rpartition("/")[0] or "/"
    return f"→ {folder}"


def _delete(entry, detail):
    return _("The folder and everything in it") if detail.get("folder") else ""


def _upload(entry, detail):
    return _sentences([_size(detail.get("size")), _("Replaced the file that was there") if detail.get("replaced") else ""])


def _edit(entry, detail):
    return _sentences([_size(detail.get("size")), _("New file") if detail.get("new") else "",
                       "OnlyOffice" if detail.get("via") == "onlyoffice" else ""])


def _short_link(entry, detail):
    return _expiry(detail.get("expires_at"))


def _create_share(entry, detail):
    return _sentences([_names(detail.get("items")), _("With a password") if detail.get("password") else "",
                       _expiry(detail.get("expires_at"))])


def _delete_share(entry, detail):
    return _names(detail.get("items"))


def _clear_shares(entry, detail):
    count = detail.get("count") or 0
    if detail.get("everyone"):
        return ngettext("%(count)s share, everybody's", "%(count)s shares, everybody's", count) % {"count": count}
    return ngettext("%(count)s share", "%(count)s shares", count) % {"count": count}


def _create_user(entry, detail):
    return _parts([_("Disabled") if detail.get("enabled") is False else "",
                   _("No password") if detail.get("password") is False else ""])


def _update_user(entry, detail):
    parts = [_change(_("Username"), detail.get("username")), _change(_("Nickname"), detail.get("nickname")),
             _("New password") if detail.get("password") else ""]
    if "enabled" in detail:
        parts.append(_("Enabled") if detail["enabled"] else _("Disabled"))
    if permissions := _added_removed(detail.get("permissions")):
        parts.append(_("Default permissions: %(changes)s") % {"changes": permissions})
    return _sentences(parts)


def _copy_user(entry, detail):
    return _("Copied from %(username)s") % {"username": detail["from"]} if detail.get("from") else ""


def _grants(entry, detail):
    parts = []
    for item in detail.get("storages") or []:
        if not isinstance(item, dict):
            continue
        change = item.get("change")
        if change == "added":
            what = _("allowed")
        elif change == "removed":
            what = _("not allowed any more")
        else:
            what = _parts([
                (_("Enabled") if item["enabled"] else _("Disabled")) if "enabled" in item else "",
                _change(_("Root folder"), item.get("root")),
                _added_removed(item.get("permissions")),
            ])
        parts.append(_("%(storage)s: %(changes)s") % {"storage": item.get("name") or "?", "changes": what})
    return _sentences(parts)


def _create_storage(entry, detail):
    return _type_label(detail.get("type"))


def _update_storage(entry, detail):
    from apps.storages.models import StorageSource

    labels = _model_labels(StorageSource)
    parts = [_change(labels["name"], detail.get("name")), _change(labels["key"], detail.get("key"))]
    others = [labels.get(name, name) for name in detail.get("fields") or []]
    if params := _param_labels(detail.get("type"), detail.get("params")):
        others.append(_("settings (%(params)s)") % {"params": params})
    if others:
        parts.append(_("Changed: %(fields)s") % {"fields": _join(others)})
    return _sentences(parts)


def _copy_storage(entry, detail):
    return _('Copied from "%(name)s"') % {"name": detail["from"]} if detail.get("from") else ""


def _rotate(entry, detail):
    return _("Its permanent download links made before stop working")


def _rules(entry, detail):
    parts = []
    for key, text in (("added", ngettext("%(count)s added", "%(count)s added", detail.get("added") or 0)),
                      ("changed", ngettext("%(count)s changed", "%(count)s changed", detail.get("changed") or 0)),
                      ("removed", ngettext("%(count)s removed", "%(count)s removed", detail.get("removed") or 0))):
        if detail.get(key):
            parts.append(text % {"count": detail[key]})
    return _join(parts)


def _settings(entry, detail):
    labels, _group = _settings_labels(detail.get("group") or "")
    return _join(capfirst(labels.get(name, name)) for name in detail.get("fields") or [])


def _update_provider(entry, detail):
    from apps.sso.models import Provider

    labels = _model_labels(Provider)
    fields = _join(labels.get(name, name) for name in detail.get("fields") or [])
    return _sentences([detail.get("provider"), _("Changed: %(fields)s") % {"fields": fields} if fields else ""])


def _delete_short_links(entry, detail):
    count = detail.get("count") or 0
    parts = [ngettext("%(count)s short link", "%(count)s short links", count) % {"count": count}]
    if detail.get("expired"):
        parts.append(_("the expired ones"))
        if detail.get("logs"):
            parts.append(ngettext("with %(count)s download log entry", "with %(count)s download log entries",
                                  detail["logs"]) % {"count": detail["logs"]})
    return _parts(parts)


def _delete_download_logs(entry, detail):
    count = detail.get("count") or 0
    return ngettext("%(count)s entry", "%(count)s entries", count) % {"count": count}


def _ip_geo(entry, detail):
    return _("Turned on") if detail.get("enabled") else _("Turned off")


def _ip_geo_download(entry, detail):
    from apps.ipgeo.sources import EDITIONS

    edition = EDITIONS.get(detail.get("edition"))  # which database: not in entries of before there was a choice
    day = _day(detail.get("built_at"))
    return _sentences([edition.file if edition else "", _("Data of %(date)s") % {"date": day} if day else ""])


def _ip_geo_fill(entry, detail):
    changed = detail.get("changed") or 0
    if detail.get("redo"):
        looked = detail.get("addresses") or 0
        return _sentences([
            ngettext("Looked up %(count)s IP again", "Looked up %(count)s IPs again", looked) % {"count": looked},
            ngettext("the location of %(count)s record changed", "the locations of %(count)s records changed", changed) % {"count": changed},
        ])
    return ngettext("%(count)s record filled in", "%(count)s records filled in", changed) % {"count": changed}


def _copy_task(entry, detail):
    """Which task, and where it copies to; how it copies, for a new one; which run, for a resumed one."""
    from apps.copytasks.models import CopyTask

    parts = [_("Task #%(id)s") % {"id": detail["task"]} if detail.get("task") else ""]
    if entry.to_path:
        parts.append(_("→ %(storage)s: %(path)s") % {"storage": detail.get("to_storage") or "?", "path": entry.to_path})
    if entry.kind == Kind.CREATE_COPY_TASK and isinstance(detail.get("threads"), int):
        policies = dict(CopyTask.Policy.choices)
        speed = detail.get("speed_limit")
        parts.append(_parts([
            ngettext("%(count)s thread", "%(count)s threads", detail["threads"]) % {"count": detail["threads"]},
            _("speed limit %(speed)s/s") % {"speed": _size(speed)} if isinstance(speed, int) and speed > 0 else _("no speed limit"),
            _("same file: %(policy)s") % {"policy": policies.get(detail.get("same_file"), "?")},
            _("same name: %(policy)s") % {"policy": policies.get(detail.get("same_name"), "?")},
            ngettext("%(count)s retry", "%(count)s retries", detail.get("retries") or 0) % {"count": detail.get("retries") or 0},
        ]))
    if entry.kind == Kind.RESUME_COPY_TASK and isinstance(detail.get("run"), int):
        parts.append(_("run %(run)s") % {"run": detail["run"]})
    return _sentences(parts)


def _reset_admin(entry, detail):
    return _parts([_("New password"), _("two-step verification turned off") if detail.get("totp") else "",
                   _("enabled again") if detail.get("enabled") else "",
                   _("can open the Manage page again") if detail.get("staff") else ""])


DESCRIBE = {
    Kind.LOGIN: _login, Kind.LOGIN_FAILED: _login_failed, Kind.CHANGE_PASSWORD: _password,
    Kind.LINK_SSO: _provider, Kind.UNLINK_SSO: _provider, Kind.SSO_REGISTER: _provider,
    Kind.MKDIR: _mkdir, Kind.RENAME: _rename, Kind.MOVE: _relocate, Kind.COPY: _relocate, Kind.DELETE: _delete,
    Kind.UPLOAD: _upload, Kind.EDIT: _edit,
    Kind.SHORT_LINK: _short_link, Kind.CREATE_SHARE: _create_share, Kind.DELETE_SHARE: _delete_share,
    Kind.CLEAR_SHARES: _clear_shares,
    Kind.CREATE_USER: _create_user, Kind.UPDATE_USER: _update_user, Kind.COPY_USER: _copy_user,
    Kind.SET_GRANTS: _grants, Kind.LINK_IDENTITY: _identity, Kind.UNLINK_IDENTITY: _identity,
    Kind.CREATE_STORAGE: _create_storage, Kind.UPDATE_STORAGE: _update_storage, Kind.DELETE_STORAGE: _create_storage,
    Kind.COPY_STORAGE: _copy_storage, Kind.ROTATE_LINK_SECRET: _rotate,
    Kind.UPDATE_FILTERS: _rules, Kind.UPDATE_PASSWORDS: _rules, Kind.UPDATE_READMES: _rules,
    Kind.CREATE_COPY_TASK: _copy_task, Kind.STOP_COPY_TASK: _copy_task, Kind.RESUME_COPY_TASK: _copy_task,
    Kind.UPDATE_SETTINGS: _settings, Kind.CREATE_PROVIDER: _provider, Kind.UPDATE_PROVIDER: _update_provider,
    Kind.DELETE_PROVIDER: _provider, Kind.DELETE_SHORT_LINKS: _delete_short_links,
    Kind.DELETE_DOWNLOAD_LOGS: _delete_download_logs, Kind.UPDATE_IP_GEO: _ip_geo,
    Kind.DOWNLOAD_IP_GEO: _ip_geo_download, Kind.FILL_IP_GEO: _ip_geo_fill, Kind.RESET_ADMIN: _reset_admin,
}


def describe(entry):
    """The text of the entry's 详情, in the active language."""
    work = DESCRIBE.get(entry.kind)
    detail = entry.detail if isinstance(entry.detail, dict) else {}
    return str(work(entry, detail)) if work else ""


def settings_group_label(group):
    return _settings_labels(group)[1]
