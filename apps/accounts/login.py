"""Logging in, in one step or in several: POST /api/v1/session/ (apps/accounts/views.py SessionView).

With no steps registered, the username and password log in at once. Later features add steps,
from their AppConfig.ready(), without changing the view:

    login.register(login.Step("totp", "两步验证", needed=..., check=...))

- A step with before_password=True (an image captcha) is checked in the same request as the
  password, before it: needed(request, None) says whether to ask, check(request, None, data) checks
  the fields the page sent. When they are missing, check raises StepRequired, and the answer is
  200 {"authenticated": false, "step": "<name>", "detail": ...}: the page shows the step and sends
  the password again with its fields.
- A step after the password (two-factor authentication) is asked once the password is right and
  needed(request, user) says so. The password's success is kept in the session for a few minutes
  (PENDING_SECONDS), and the answer is the same 200 {"step": "<name>"}; the page then sends only
  {"step": "<name>", ...its fields}, which check(request, user, data) checks. A wrong answer is a
  400 and the step can be tried again, STEP_TRIES times at most before the password is asked for
  again (the login throttle counts every try too); when every step is done the user is logged in:
  201 {"authenticated": true, "user": ...}.

check() raises rest_framework's ValidationError with Chinese messages. The Django admin's login
page doesn't know about steps: it refuses accounts that need one (apps/accounts/admin.py).
"""

import time
from dataclasses import dataclass
from typing import Callable

from django.contrib.auth import get_user_model, login
from django.utils.translation import gettext as _
from rest_framework import serializers

PENDING_KEY = "hfile_login_pending"
PENDING_SECONDS = 300
STEP_TRIES = 5  # wrong answers to the steps of one login before it starts again from the password


class StepRequired(Exception):
    """A before_password step whose fields weren't sent: the page is asked to show it."""


@dataclass(frozen=True)
class Step:
    name: str  # what the page is told: "totp", "captcha", ...
    label: str  # in Chinese, for messages: "两步验证"
    needed: Callable  # (request, user or None) -> bool
    check: Callable  # (request, user or None, data) -> None; raises ValidationError or StepRequired
    before_password: bool = False


STEPS: list[Step] = []


def register(step):
    if any(existing.name == step.name for existing in STEPS):
        raise ValueError(f"a login step named {step.name!r} exists already")
    STEPS.append(step)


def unregister(name):
    STEPS[:] = [step for step in STEPS if step.name != name]


def _step(name):
    return next((step for step in STEPS if step.name == name), None)


def ask(step):
    return {
        "authenticated": False,
        "step": step.name,
        "before_password": step.before_password,  # then the page sends the password again with it
        "detail": _("One more step to log in: %(label)s.") % {"label": step.label},
    }


def before_password(request, data):
    """Checks the steps that come before the password. Returns None when they pass (or none is
    needed), else the answer that asks for one."""
    for step in STEPS:
        if step.before_password and step.needed(request, None):
            try:
                step.check(request, None, data)
            except StepRequired:
                return ask(step)
    return None


def after_password(request, user, *, source=None):
    """The user's password was right: logs them in, or keeps that and asks for the first step they
    need. Returns (answer, logged_in).

    Single sign-on calls this too, once the provider has vouched for the user (it is the first factor;
    the steps before the password, the captcha, don't apply). `source` is where the login began, for the
    login log ("sso"): it is kept while the login waits for a step."""
    names = [step.name for step in STEPS if not step.before_password and step.needed(request, user)]
    if not names:
        return finish(request, user, source=source), True
    request.session[PENDING_KEY] = {
        "user": user.pk,
        "steps": names,
        # A password changed meanwhile ends what was begun with the old one.
        "hash": user.get_session_auth_hash(),
        "until": time.time() + PENDING_SECONDS,
        "source": source,
    }
    return ask(_step(names[0])), False


def pending(request):
    """The step a login begun in this session waits for, or None."""
    begun = request.session.get(PENDING_KEY)
    if not begun or begun.get("until", 0) < time.time() or not begun.get("steps"):
        return None
    return _step(begun["steps"][0])


def continue_step(request, name, data):
    """POST {"step": name, ...}: checks it, and logs in after the last one. Returns (answer, logged_in);
    raises ValidationError."""
    begun = request.session.get(PENDING_KEY)
    expired = serializers.ValidationError({"step": [_("The login has expired. Please enter your username and password again.")]})
    if not begun or begun.get("until", 0) < time.time() or not begun.get("steps"):
        request.session.pop(PENDING_KEY, None)
        raise expired
    step = _step(begun["steps"][0])
    if step is None or name != step.name:
        raise serializers.ValidationError({"step": [_('The step needed now is "%(step)s".') % {"step": step.label if step else "?"}]})
    if begun.get("source"):
        request.login_source = begun["source"]  # the login log counts the step's answers there too
    user = get_user_model()._default_manager.filter(pk=begun["user"], is_active=True).first()
    if user is None or user.get_session_auth_hash() != begun["hash"]:
        request.session.pop(PENDING_KEY, None)
        raise expired
    try:
        step.check(request, user, data)
    except serializers.ValidationError:
        begun["tries"] = begun.get("tries", 0) + 1
        if begun["tries"] >= STEP_TRIES:
            request.session.pop(PENDING_KEY, None)
            raise serializers.ValidationError({"step": [_("Too many wrong tries. Please enter your username and password again.")]}) from None
        request.session[PENDING_KEY] = begun
        raise
    begun["steps"] = begun["steps"][1:]
    if begun["steps"]:
        request.session[PENDING_KEY] = begun
        return ask(_step(begun["steps"][0])), False
    return finish(request, user, source=begun.get("source")), True


def finish(request, user, *, source=None):
    from .serializers import UserSerializer

    request.session.pop(PENDING_KEY, None)
    if source:
        request.login_source = source  # loginlog.source_of(), for the row user_logged_in writes
    login(request, user)
    return {"authenticated": True, "user": UserSerializer(user).data}


def needs_steps(request, user):
    """Whether this user's login asks for a step after the password (the Django admin refuses them)."""
    return any(not step.before_password and step.needed(request, user) for step in STEPS)
