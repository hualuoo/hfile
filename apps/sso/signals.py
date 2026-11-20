"""Receivers of the sso app (apps.py)."""

from django.utils import timezone


def on_login(sender, request, user, **kwargs):
    """Every login: a session begun for a single sign-on (flow.SHORT_KEY: it had a short life while it held
    only that) gets the normal lifetime back, whichever way the login happened; django's login() kept the
    custom expiry through cycle_key.

    A login that began with single sign-on and waited for a step (two-step verification) is done: the
    identity it began with was used just now (flow.py keeps which one in the session)."""
    if request is None:
        return
    from .flow import LOGIN_IDENTITY_KEY, SHORT_KEY
    from .models import Identity

    if request.session.pop(SHORT_KEY, None):
        request.session.set_expiry(None)
    if getattr(request, "login_source", "") != "sso":
        return
    pk = request.session.pop(LOGIN_IDENTITY_KEY, None)
    if isinstance(pk, int):
        Identity.objects.filter(pk=pk, user=user).update(last_login_at=timezone.now())
