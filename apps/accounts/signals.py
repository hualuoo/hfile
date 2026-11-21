"""On every login (the API, the Django admin): the IP it came from and an entry in the activity log. On every
failed password login: an entry in the activity log. And the cleanup job of the sessions that have ended."""

from importlib import import_module

from django.conf import settings
from django.dispatch import Signal

from apps.core.net import client_ip
from apps.ipgeo.lookup import locate

# Sent by the user management after it made a copy of an account, with source= and target= (User
# instances), inside the same transaction. The copy may do on each storage source what the source
# may: the storages app connects to it and copies the grants.
user_copied = Signal()

# Ended sessions stay in the database until something deletes them (Django leaves that to its
# `clearsessions` command): the cleanup (apps.core.cleanup) does, this many at most per run, the rest the
# next time, so that its transaction is short.
SESSIONS_PER_RUN = 20000
SESSIONS_BATCH = 500  # per query


def remove_expired_sessions(now):
    """The cleanup job (apps.core.cleanup): the sessions that have ended at `now`, also those of visitors who
    never log in (a guest's uploads, unlocked folders, a single sign-on start)."""
    store = import_module(settings.SESSION_ENGINE).SessionStore
    if not hasattr(store, "get_model_class"):  # signed cookies (setup mode): nothing is stored
        return 0
    expired = store.get_model_class().objects.filter(expire_date__lt=now)
    keys = list(expired.values_list("pk", flat=True)[:SESSIONS_PER_RUN])
    # The same condition again: a session saved since it was listed has a later expiry, and stays.
    return sum(expired.filter(pk__in=keys[start:start + SESSIONS_BATCH]).delete()[0]
               for start in range(0, len(keys), SESSIONS_BATCH))


def on_login(sender, request, user, **kwargs):
    """Stores the address the login came from, and its location while IP 归属地 is on (Django's own
    receiver stores the time, last_login), and logs the login (loginlog.py)."""
    if request is None:
        return
    from . import loginlog

    ip = client_ip(request)
    user.last_login_ip = ip
    user.last_login_location = locate(ip)
    type(user).objects.filter(pk=user.pk).update(last_login_ip=ip, last_login_location=user.last_login_location)
    loginlog.record(request, success=True, user=user)


def on_login_failed(sender, credentials, request=None, **kwargs):
    """A password that didn't log in (authenticate() found no account it opens): the activity log says
    whom it was for and why. Django has replaced the password in `credentials` already."""
    if request is None:  # authenticate() called outside a request
        return
    from . import loginlog
    from .backends import find_user

    typed = loginlog.typed_username(credentials)
    account = find_user(typed)
    loginlog.record(request, success=False, username=typed, user=account, reason=loginlog.failure_reason(account))
