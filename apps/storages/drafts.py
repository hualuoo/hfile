"""What the storage form keeps on the server while an admin edits it (the admin's session).

- A draft holds what the form must not see: secrets an OAuth authorisation or a QR-code login gave
  (encrypted with apps.core.secrets), the arguments of a QR-code poll (115's PKCE code_verifier), and a copy
  of the form's other values, so the form can come back after the browser went to the provider. The
  form names its draft by id; saving the form uses the draft's secrets and drops it.
- An OAuth flow waits for its callback under a random `state`: the storage, the draft, the app's id and
  secret and the redirect URI. Nothing of it travels in a URL but the state, and each state works once.

Both expire (a draft after six hours, a flow after fifteen minutes) and are few per session.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from apps.core import secrets as core_secrets

from . import config as storage_config

DRAFTS_KEY = "hfile.storage_drafts"
OAUTH_KEY = "hfile.storage_oauth"
DRAFT_TTL = 6 * 3600
DRAFT_MAX = 8
OAUTH_TTL = 15 * 60
OAUTH_MAX = 5
POLLS_MAX = 4
FORM_MAX = 16 * 1024  # characters of the form's copy (its values without the secrets)


def _now() -> float:
    return time.time()


def _drafts(request) -> dict[str, dict[str, Any]]:
    drafts = dict(request.session.get(DRAFTS_KEY, {}))
    now = _now()
    return {key: value for key, value in drafts.items()
            if isinstance(value, dict) and now - value.get("touched", 0) < DRAFT_TTL}


def _store(request, drafts: dict[str, dict[str, Any]]) -> None:
    while len(drafts) > DRAFT_MAX:
        oldest = min(drafts, key=lambda key: drafts[key].get("touched", 0))
        drafts.pop(oldest)
    request.session[DRAFTS_KEY] = drafts


def get(request, draft_id, *, storage_id: int | None, type_: str) -> dict[str, Any] | None:
    """The draft of this form (the same storage, or the same new type), or None."""
    if not isinstance(draft_id, str) or not draft_id:
        return None
    draft = _drafts(request).get(draft_id)
    if draft is None or draft.get("storage") != storage_id or draft.get("type") != type_:
        return None
    return draft


def ensure(request, draft_id, *, storage_id: int | None, type_: str) -> dict[str, Any]:
    """The form's draft, or a new one."""
    draft = get(request, draft_id, storage_id=storage_id, type_=type_)
    if draft is None:
        draft = {"id": secrets.token_urlsafe(12), "storage": storage_id, "type": type_, "secrets": {},
                 "changes": {}, "polls": {}, "form": {}, "touched": _now()}
    return draft


def save(request, draft: dict[str, Any]) -> None:
    drafts = _drafts(request)
    draft["touched"] = _now()
    drafts[draft["id"]] = draft
    _store(request, drafts)


def drop(request, draft_id) -> None:
    if not isinstance(draft_id, str):
        return
    drafts = _drafts(request)
    if drafts.pop(draft_id, None) is not None:
        _store(request, drafts)


def secret_values(draft: dict[str, Any] | None, cls) -> dict[str, Any]:
    """The draft's secrets, decrypted (a secret that can't be read any more is left out)."""
    if not draft:
        return {}
    params = storage_config.params(cls)
    values = {}
    for key, stored in (draft.get("secrets") or {}).items():
        param = params.get(key)
        if param is None or not param.secret:
            continue
        try:
            values[key] = storage_config.decode_value(param, stored)
        except core_secrets.UnreadableSecret:
            continue
    return values


def put(draft: dict[str, Any], cls, changes: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Keep the secret changes in the draft; returns (their keys, the changes that aren't secret)."""
    params = storage_config.params(cls)
    stored = dict(draft.get("secrets") or {})
    plain = dict(draft.get("changes") or {})
    secret_keys, others = [], {}
    for key, value in changes.items():
        param = params.get(key)
        if param is None:
            continue
        if param.secret:
            stored[key] = storage_config.encode_value(param, value)
            secret_keys.append(key)
        else:
            plain[key] = value
            others[key] = value
    draft["secrets"] = stored
    draft["changes"] = plain
    return secret_keys, others


def put_poll(draft: dict[str, Any], args: dict[str, Any]) -> str:
    """Keep a poll's arguments on the server; the browser polls with the returned token."""
    token = secrets.token_urlsafe(12)
    polls = dict(draft.get("polls") or {})
    polls[token] = core_secrets.encrypt(json.dumps(args))
    while len(polls) > POLLS_MAX:
        polls.pop(next(iter(polls)))
    draft["polls"] = polls
    return token


def take_poll(draft: dict[str, Any], token) -> dict[str, Any] | None:
    polls = dict(draft.get("polls") or {})
    stored = polls.pop(token, None) if isinstance(token, str) else None
    draft["polls"] = polls
    if stored is None:
        return None
    try:
        args = json.loads(core_secrets.decrypt(stored))
    except (core_secrets.UnreadableSecret, ValueError):
        return None
    return args if isinstance(args, dict) else None


def set_form(draft: dict[str, Any], cls, form: Any) -> None:
    """A copy of the form's values, to fill it in again after a full-page redirect; secrets are left
    out whatever the browser sent."""
    if not isinstance(form, dict) or len(json.dumps(form, ensure_ascii=False)) > FORM_MAX:
        return
    form = dict(form)
    if isinstance(form.get("config"), dict):
        secret = set(storage_config.secret_keys(cls))
        form["config"] = {key: value for key, value in form["config"].items() if key not in secret}
    draft["form"] = form


def find(request, draft_id) -> dict[str, Any] | None:
    """A draft of this session by its id, whatever form it is for."""
    return _drafts(request).get(draft_id) if isinstance(draft_id, str) else None


def shown(draft: dict[str, Any]) -> dict[str, Any]:
    """What the browser may see of a draft."""
    return {
        "id": draft["id"],
        "storage": draft.get("storage"),
        "type": draft.get("type"),
        "secrets": sorted((draft.get("secrets") or {}).keys()),
        "changes": dict(draft.get("changes") or {}),
        "form": dict(draft.get("form") or {}),
    }


# --- OAuth flows waiting for their callback ------------------------------------------------------------

def start_oauth(request, *, draft: dict[str, Any], cls, config: dict[str, Any], redirect_uri: str,
                setup: bool = False) -> str:
    flows = {key: value for key, value in dict(request.session.get(OAUTH_KEY, {})).items()
             if isinstance(value, dict) and _now() - value.get("created", 0) < OAUTH_TTL}
    state = secrets.token_urlsafe(32)
    flows[state] = {
        "draft": draft["id"], "storage": draft.get("storage"), "type": draft.get("type"),
        "config": storage_config.encode(cls, config), "redirect_uri": redirect_uri, "created": _now(),
        "setup": bool(setup),
    }
    while len(flows) > OAUTH_MAX:
        oldest = min(flows, key=lambda key: flows[key].get("created", 0))
        flows.pop(oldest)
    request.session[OAUTH_KEY] = flows
    return state


def take_oauth(request, state) -> dict[str, Any] | None:
    """The flow of a callback's state, once; None when unknown or expired."""
    flows = dict(request.session.get(OAUTH_KEY, {}))
    flow = flows.pop(state, None) if isinstance(state, str) else None
    request.session[OAUTH_KEY] = flows
    if not isinstance(flow, dict) or _now() - flow.get("created", 0) >= OAUTH_TTL:
        return None
    return flow
