"""For the tests: the activity log's logins read as the login log had them, and the entries of a kind."""

from dataclasses import dataclass

from .models import ActivityLog

Kind = ActivityLog.Kind


@dataclass
class Login:
    result: str  # "success" or "failure"
    reason: str
    source: str
    user: object  # who logged in; for a failed login the account it was for, None when there was none
    username: str  # what was typed (a login: the user's name)
    ip: str
    user_agent: str


def _login(entry):
    detail = entry.detail or {}
    success = entry.kind == Kind.LOGIN
    return Login(
        result="success" if success else "failure",
        reason="" if success else detail.get("reason", ""),
        source=detail.get("source", ""),
        user=entry.actor if success else entry.target,
        username=entry.actor_name if success else detail.get("username") or entry.target_name,
        ip=entry.ip,
        user_agent=entry.user_agent,
    )


def logins():
    """Every login and failed login, oldest first."""
    return [_login(entry) for entry in ActivityLog.objects.filter(kind__in=(Kind.LOGIN, Kind.LOGIN_FAILED)).order_by("id")]


def only_login(test):
    found = logins()
    test.assertEqual(len(found), 1, found)
    return found[0]


def entries(kind=None):
    """The entries (of one kind), oldest first."""
    found = ActivityLog.objects.order_by("id")
    return list(found.filter(kind=kind) if kind else found)
