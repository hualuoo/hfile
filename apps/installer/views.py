"""The installer's pages, in setup mode only (apps/installer/setup_urls.py; config/settings.py).

GET  /install/         the form, or, once config.toml has a secret_key, a page saying the program restarts
POST /install/         checks everything, installs (service.py), writes config.toml and asks for a restart
POST /install/check/   checks one step of the form, before the page goes on to the next (install.js);
                       the database's step also tests the database, as a dry run (service.preflight)

Every other address redirects to /install/; the API answers 503. After a successful install
the address of the Django admin is shown once, in the answer to the POST, and never on a GET:
until the program has restarted, anybody who opens the site would see that page.

The form has no CSRF check, nor has /install/check/. Before installing, the site can't know its own address: behind a
reverse proxy that ends https, Django sees http (and maybe another Host), so the browser's Origin
would never match and the form couldn't be sent at all. The install token does that job instead:
a page on another site can't know it, and without it the form does nothing. /install/check/
doesn't even look at the other fields without it: with them it connects to the server they name.
"""

import logging
import threading
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.text import format_lazy
from django.utils.translation import gettext as _, gettext_lazy
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from config import admin_path, configfile
from apps.core.net import client_ip

from . import service, token
from .forms import InstallForm, host_of

TOKEN_HELP = gettext_lazy("It is printed in the command-line window that started the program, and "
                          "saved in install-token.txt in the folder of the settings file (config.toml).")
WRONG_TOKEN = format_lazy(gettext_lazy("The install token is wrong. {help}"), help=TOKEN_HELP)
# Headers only a reverse proxy adds, never a browser: one of them means there is a proxy in front.
PROXY_HEADERS = ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_REAL_IP", "HTTP_FORWARDED")


def request_origin(request):
    """The address as this program sees it. Behind a proxy that ends https it says http, which is
    why the page replaces it with the browser's own (install.js)."""
    return f"{request.scheme}://{request.get_host()}"


def not_installed(request, *args, **kwargs):
    if request.path.startswith("/api/"):
        response = JsonResponse({"detail": _("The site isn't installed yet: open /install/ first and follow the setup wizard.")}, status=503)
        # Expected, not an error for the console: the installer's last page asks every 1.5 s until
        # the program has started again (install.js). Django logs every 5xx unless told it has been.
        response._has_been_logged = True
        return response
    return redirect("install")


def _installed():
    """config.toml has a secret_key: the installer wrote it, and the program hasn't started again yet."""
    try:
        return bool(configfile.load(settings.CONFIG_FILE)["site"]["secret_key"])
    except configfile.ConfigError:
        return False


# One install at a time, from the check "installed already?" to writing config.toml: two tabs sending
# the form at once would each make a database and an admin, and config.toml keep only the second.
logger = logging.getLogger("installer")
_installing = threading.Lock()


@method_decorator(csrf_exempt, name="dispatch")  # the install token instead (see above)
@method_decorator(never_cache, name="dispatch")
class InstallView(View):
    template_name = "installer/install.html"

    def render_form(self, request, form, *, problem="", log="", status=200):
        origin = request_origin(request)
        # install.js puts the browser's address into 网站地址 while it still holds this guess.
        form.fields["url"].widget.attrs["data-guess"] = origin
        try:
            token.get()  # made now, so the page can say where it is
            token_problem = ""
        except OSError as error:
            token_problem = _("%(problem)s HFile can't be installed without it: check this folder, or "
                              "put the settings file in another folder with --config, and run it again.") % {
                "problem": token.problem(error)
            }
        context = {
            "form": form,
            "problem": problem,
            "log": log,
            "token_problem": token_problem,
            "token_help": TOKEN_HELP,
            "origin": origin,
            "scheme": request.scheme,
            "admin_path_example": admin_path.random_path(),
            "sqlite_note": (
                _("A relative path starts from the folder of the settings file (%(folder)s); "
                  "missing folders are created. A file that exists must have no other tables.")
                % {"folder": Path(settings.CONFIG_FILE).parent}
            ),
        }
        return render(request, self.template_name, context, status=status)

    def get(self, request):
        if _installed():
            return render(request, "installer/done.html", {"restarting": None})
        # What config.toml says already (it may have been filled in by hand), but never its password:
        # anybody can open this page.
        database = settings.CONFIG["database"]
        behind_proxy = any(header in request.META for header in PROXY_HEADERS)
        initial = {
            "url": settings.CONFIG["site"]["url"] or request_origin(request),
            "hosts": host_of(request.get_host()),
            "proxies": settings.CONFIG["server"]["proxies"] or int(behind_proxy),
            "token": request.GET.get("token", ""),
            **{f"db_{key}": database[key] for key in ("engine", "file", "host", "port", "name", "user")},
        }
        return self.render_form(request, InstallForm(initial=initial))

    def post(self, request):
        with _installing:
            return self._post(request)

    def _post(self, request):
        if _installed():
            return render(request, "installer/done.html", {"restarting": None})
        form = InstallForm(request.POST)
        if not form.is_valid():
            return self.render_form(request, form, status=400)
        data = form.cleaned_data
        if not token.check(data["token"]):
            _wrong_token(request)
            form.add_error("token", WRONG_TOKEN)
            return self.render_form(request, form, status=400)

        db = form.database()
        try:
            service.preflight(db)
            admin = service.install(
                username=data["username"],
                password=data["password1"],
                site_name=data["site_name"],
                db=db,
            )
        except service.InstallError as error:
            form.add_error(error.field if error.field in form.fields else None, error.message)
            return self.render_form(request, form, log=error.log, status=400)

        path = data["admin_path"] or admin_path.random_path()
        # A page or a script from before these two fields doesn't send them: config.toml keeps its values.
        url = data["url"] if "url" in form.data else settings.CONFIG["site"]["url"]
        proxies = settings.CONFIG["server"]["proxies"] if data["proxies"] is None else data["proxies"]
        changes = {
            "site": {
                "secret_key": settings.SECRET_KEY,  # this process's, now the site's
                # The browser's address: CSRF_TRUSTED_ORIGINS, so its forms are accepted behind a proxy.
                "url": url,
                "allowed_hosts": data["hosts"],
                "admin_path": path,
            },
            "server": {"proxies": proxies},
            "database": db.config(),
        }
        # The rest of the file ([server]: the port and IP this is running on; the other engine's
        # [database] settings; debug, the folders, [upgrade]) stays as it is.
        try:
            configfile.update(settings.CONFIG_FILE, changes)
            config_problem, config_text = "", ""
        except (OSError, configfile.ConfigError) as error:
            config_problem = (error.strerror or str(error)) if isinstance(error, OSError) else str(error)
            values = {section: dict(items) for section, items in settings.CONFIG.items()}
            for section, items in changes.items():
                values[section].update(items)
            config_text = configfile.render(values)
        if not config_problem:
            token.remove()
        context = {
            "admin": admin,
            "origin": url or request_origin(request),
            "admin_path": path,
            "config_file": settings.CONFIG_FILE,
            "config_problem": config_problem,
            "config_text": config_text,
            "debug": settings.DEBUG,
            "restarting": False if config_problem else service.restart_soon(),
        }
        return render(request, "installer/done.html", context)


def _answer(data, status=200):
    response = JsonResponse(data, status=status)
    # Not in the log: a step that isn't right yet is what the page is for (a wrong token is logged
    # where it is found, _wrong_token).
    response._has_been_logged = True
    return response


def _wrong_token(request):
    """Someone without the 安装口令 tried the installer: worth a line in the console (Django leaves 4xx out)."""
    logger.warning("Wrong install token: %s %s (from %s)", request.method, request.path, client_ip(request) or "?")


@method_decorator(csrf_exempt, name="dispatch")  # the install token instead (see the top)
@method_decorator(never_cache, name="dispatch")
class CheckView(View):
    """POST /install/check/: the form as it is, and `step`, one of InstallForm.STEPS. Answers
    {"ok": true} (the database's step: and "message", what the test found), or {"ok": false} with
    "errors" ({field: [message, ...]}, as the form says them), or "message" and "field" (the test's
    InstallError), or "step": "token" when it is the token that is wrong."""

    def post(self, request):
        if _installed():
            return _answer({"ok": False, "message": _("Installed already; waiting for the program to restart.")}, status=409)
        step = request.POST.get("step", "")
        if step not in InstallForm.STEPS:
            return _answer({"ok": False, "message": _("Unknown step to check.")}, status=400)
        if not token.check(request.POST.get("token", "")):
            _wrong_token(request)
            return _answer({"ok": False, "step": "token", "errors": {"token": [str(WRONG_TOKEN)]}}, status=403)
        form = InstallForm(request.POST)
        form.is_valid()
        errors = {name: [str(error) for error in form.errors[name]] for name in InstallForm.STEPS[step] if name in form.errors}
        if errors:
            return _answer({"ok": False, "errors": errors}, status=400)
        if step != "database":
            return _answer({"ok": True})
        try:
            message = service.preflight(form.database(), dry_run=True)
        except service.InstallError as error:
            return _answer({"ok": False, "message": error.message, "field": error.field}, status=400)
        return _answer({"ok": True, "message": message})
