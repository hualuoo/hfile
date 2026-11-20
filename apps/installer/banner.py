"""What runserver and serve print when they start: where to go next, that is the Django admin's
random address, or before installing, the installer and its 安装口令 (apps/installer/token.py)."""

from django.conf import settings
from django.utils.translation import gettext as _

from . import token

WILDCARDS = {"0.0.0.0", "0", "::", ""}


def origin(host, port, scheme="http"):
    """The address to open on this machine for a server listening on host:port. Listening on "::",
    waitress takes IPv6 only (IPV6_V6ONLY), so 127.0.0.1 wouldn't answer: ::1 does."""
    if host in ("::", "[::]"):
        host = "[::1]"
    elif host in WILDCARDS:
        host = "127.0.0.1"
    elif ":" in host:  # IPv6
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def render(origin, style):
    """The text, styled with a management command's `style`."""
    if not settings.SETUP_MODE:
        return _("Django admin: %(address)s") % {"address": f"{origin}/{settings.ADMIN_PATH}/"} + "\n"
    try:
        code = token.get()
    except OSError as error:
        return "\n" + style.ERROR(_("HFile isn't installed yet. %(problem)s") % {"problem": token.problem(error)}) + "\n"
    return style.SUCCESS(
        "\n" + _("HFile isn't installed yet. Open %(address)s in a browser and follow the setup wizard.") % {"address": f"{origin}/install/?token={code}"}
        + "\n" + _("Install token: %(token)s (also saved in %(path)s)") % {"token": code, "path": token.path()} + "\n"
    )
