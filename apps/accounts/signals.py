"""On every login (the API, the Django admin): the IP it came from, the login log, and now and then a
clean-up of the sessions that have ended. On every failed password login: the login log."""

import logging
import threading
import time
from importlib import import_module

from django.conf import settings
from django.dispatch import Signal

from apps.core.net import client_ip

logger = logging.getLogger(__name__)

# Sent by the user management after it made a copy of an account, with source= and target= (User
# instances), inside the same transaction. The copy may do on each storage source what the source
# may: the storages app connects to it and copies the grants.
user_copied = Signal()

# Ended sessions stay in the database until something deletes them (Django leaves that to the
# `clearsessions` command, which nothing runs here): logging in does it, once an hour at most.
CLEAR_SESSIONS_EVERY = 3600  # seconds; the first time an hour after the program started
_sessions_cleared = time.monotonic()
_clearing = threading.Lock()


def remove_expired_sessions(now):
    """`hfile cleanup`'s job: the sessions that have ended at `now`, also those of visitors who never
    log in (a guest's uploads, unlocked folders, a single sign-on start), which a login never clears."""
    store = import_module(settings.SESSION_ENGINE).SessionStore
    if not hasattr(store, "get_model_class"):  # signed cookies (setup mode): nothing is stored
        return 0
    deleted, _ = store.get_model_class().objects.filter(expire_date__lt=now).delete()
    return deleted


def clear_expired_sessions():
    global _sessions_cleared
    if time.monotonic() - _sessions_cleared < CLEAR_SESSIONS_EVERY or not _clearing.acquire(blocking=False):
        return
    try:
        _sessions_cleared = time.monotonic()
        import_module(settings.SESSION_ENGINE).SessionStore.clear_expired()
    except Exception:  # not worth a failed login
        logger.warning("Could not delete the expired sessions", exc_info=True)
    finally:
        _clearing.release()


def on_login(sender, request, user, **kwargs):
    """Stores the address the login came from (Django's own receiver stores the time, last_login),
    and writes the login log."""
    if request is None:
        return
    from . import loginlog

    ip = client_ip(request)
    user.last_login_ip = ip
    type(user).objects.filter(pk=user.pk).update(last_login_ip=ip)
    loginlog.record(request, success=True, user=user)
    clear_expired_sessions()


def on_login_failed(sender, credentials, request=None, **kwargs):
    """A password that didn't log in (authenticate() found no account it opens): the login log says
    whom it was for and why. Django has replaced the password in `credentials` already."""
    if request is None:  # authenticate() called outside a request
        return
    from . import loginlog
    from .backends import find_user

    typed = loginlog.typed_username(credentials)
    account = find_user(typed)
    loginlog.record(request, success=False, username=typed, user=account, reason=loginlog.failure_reason(account))
