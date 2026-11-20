"""`hfile serve`, which `hfile` runs when it's given no command: the site, on the IP and port in
config.toml's [server], served by waitress (a production web server written in Python, so it goes
into the single executable). Static files come from WhiteNoise (apps/core/staticfiles.py), uploads from
apps/core/uploads.py.

config/cli.py runs this in a child process and starts it again when it exits with 3, which the
installer asks for once it has written config.toml (apps/installer/service.py restart_soon).

The order: take the port first (a port in use stops it before the database is touched: an older
HFile may still be running on it), then check or upgrade the database (config/wsgi.py,
apps/upgrades/startup.py), then serve. Ctrl+C and SIGTERM stop it the same way: no new requests, the
ones it has are answered (STOP_WAIT), then it exits; during the upgrade, the database is put back
as it was first. On Windows, which has no SIGTERM, so does Ctrl+Break.
"""

import errno
import os
import select
import shlex
import signal
import socket
import time

try:  # the limit on open files, which Windows doesn't have
    import resource
except ImportError:
    resource = None

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import gettext as _

from config import configfile, database
from config.version import VERSION
from apps.installer import banner

# A client that opens connections and never finishes a request holds one each: with waitress's
# defaults (100, for 120 s) a hundred of them shut everybody else out. select() can't watch more
# than about a thousand sockets (512 on Windows), poll() can; Windows has no poll(). Requests sent
# a byte at a time are closed by apps/installer/connections.py.
CAN_POLL = hasattr(select, "poll")
CONNECTION_LIMIT = 1000 if CAN_POLL else 500
CHANNEL_TIMEOUT = 30  # seconds a connection may stay idle, a half-sent request included
STOP_WAIT = 30  # seconds the requests in hand may take to finish once asked to stop (waitress: 5)
# A connection is an open file, and one sending a static file or an upload has that file open too. Besides
# them the site has a few of its own (the console, the listening sockets, waitress's pipe), and each thread
# a few: a database connection (three files with SQLite), a file it reads or writes, a connection to a storage.
FILES_PER_CONNECTION = 2
FILES_OF_ITS_OWN = 32
FILES_PER_THREAD = 6


def connection_limit(threads):
    """CONNECTION_LIMIT, or fewer when the process may not open files enough for them (OPS-05: systemd lets a
    service open 1024, its soft limit). The soft limit is raised first, as far as needed and the hard one allows."""
    if resource is None:
        return CONNECTION_LIMIT
    besides = FILES_OF_ITS_OWN + FILES_PER_THREAD * threads
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = min(besides + FILES_PER_CONNECTION * CONNECTION_LIMIT, hard)
    if soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        soft = wanted
    return min(CONNECTION_LIMIT, (soft - besides) // FILES_PER_CONNECTION)


def _stop(signum, frame):
    # One is enough: systemd signals every process of the service, and cli.py passes it on as well, so a
    # second SIGTERM would cut short the stopping the first one began.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(0)  # ends waitress's loop like Ctrl+C: the requests in hand still finish (run())


def _exclusive_ports():
    """Windows: waitress listens with SO_REUSEADDR, which there lets another socket bind the same port as
    well, another HFile or any program of the same user, and take some of the requests; and a port in use
    goes unnoticed. SO_EXCLUSIVEADDRUSE keeps the port to the site."""
    from waitress import wasyncore

    def exclusive(dispatcher):
        dispatcher.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

    wasyncore.dispatcher.set_reuse_addr = exclusive


def _private_folder_command(folder):
    """Windows: the command that leaves `folder`, and what is and will be in it, to this user, SYSTEM and the
    Administrators (by SID: their names are translated). The quotes keep PowerShell from reading (OI)."""
    from config import windows

    grants = " ".join(f'"*{sid}:(OI)(CI)F"' for sid in (windows.user_sid(), windows.SYSTEM, windows.ADMINISTRATORS))
    return f'icacls "{folder}" /inheritance:r /grant:r {grants}'


class Command(BaseCommand):
    @property
    def help(self):  # a property: argparse can't take a lazy text, and it is shown in the command's language
        return _("Starts the site: serves it on the IP and port set in config.toml's [server] (waitress).")

    # Not Django's system checks: some ask the database, and a database that can't be reached gets
    # a plain message from the upgrade check below instead of a traceback.
    requires_system_checks = []

    def handle(self, *args, **options):
        from waitress.server import create_server

        from apps.installer import connections

        host, port, threads = settings.SERVER["host"], settings.SERVER["port"], settings.SERVER["threads"]
        site = {}

        def application(environ, start_response):
            return site["app"](environ, start_response)

        if os.name == "nt":
            _exclusive_ports()
        try:
            server = create_server(
                application,
                host=host,
                port=port,
                threads=threads,
                ident="HFile",
                # X-Forwarded-For and -Proto reach Django as they came; whether to believe them is
                # [server] proxies (settings.py, apps/core/net.py). waitress would otherwise delete them.
                clear_untrusted_proxy_headers=False,
                # waitress reads a whole body before the site sees it. The site turns away bodies over
                # MAX_REQUEST_BYTES with a message (apps/core/middleware.py); far larger ones stop here.
                max_request_body_size=2 * settings.MAX_REQUEST_BYTES,
                connection_limit=connection_limit(threads),
                channel_timeout=CHANNEL_TIMEOUT,
                cleanup_interval=connections.CHECK_EVERY,
                asyncore_use_poll=CAN_POLL,
            )
        except (OSError, ValueError) as error:
            raise CommandError(self.listen_problem(host, port, error)) from None
        # Requests that arrive a byte at a time are closed too; without a proxy, an address may hold a
        # tenth of the connections at most (apps/installer/connections.py).
        connections.guard(server, per_address=0 if settings.SERVER["proxies"] else connections.PER_ADDRESS)

        # SIGTERM (systemd, docker stop, cli.py passing it on) stops the program as Ctrl+C does, already
        # during the upgrade below, which then puts the database back as it was.
        signal.signal(signal.SIGTERM, _stop)
        if os.name == "nt":
            signal.signal(signal.SIGBREAK, _stop)
        # While it runs: restore and upgrade see it (configfile.in_use) and ask to stop it first.
        self.serving = configfile.hold_while_serving(settings.CONFIG_FILE)
        # Only now, with the port ours: if an older HFile were still running on it, upgrading the
        # database under it would break it.
        from config.wsgi import application as django_application  # checks or upgrades the database
        from apps.core.access_log import wrapped

        site["app"] = wrapped(django_application)  # a line per request, with [server] log_requests
        if not settings.SETUP_MODE:  # downloads through the site on threads of their own (the installer has none)
            from apps.files import pool

            pool.install(server)
        self.stdout.write(self.started(host, port))
        self.stdout.flush()  # a pipe (systemd, docker) would otherwise hold it back
        self.run(server)
        self.stdout.write(_("HFile has stopped."))
        self.stdout.flush()

    @staticmethod
    def run(server):
        """waitress's server.run(), waiting longer for the requests in hand when it stops: their
        threads write their answers themselves (waitress sends what it can from the thread)."""
        from waitress.server import MultiSocketServer  # a host name with several addresses

        from apps.files import pool

        sockets = server.map if isinstance(server, MultiSocketServer) else server._map
        try:
            server.asyncore.loop(timeout=server.adj.asyncore_loop_timeout, map=sockets, use_poll=server.adj.asyncore_use_poll)
        except (SystemExit, KeyboardInterrupt):
            # A SIGTERM would now cut the waiting short: cli.py sends one SIGINT_GRACE seconds after a Ctrl+C,
            # which reached this process too (OPS-01). A second Ctrl+C still does, for whoever wants that.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            deadline = time.monotonic() + STOP_WAIT
            server.task_dispatcher.shutdown(timeout=STOP_WAIT)
            pool.shutdown(timeout=max(0.0, deadline - time.monotonic()))  # the downloads go on meanwhile too

    def started(self, host, port):
        origin = banner.origin(host, port)
        lines = [
            self.style.SUCCESS(_("HFile %(version)s is serving on %(address)s. Press Ctrl+C to stop.") % {
                "version": VERSION, "address": f"{host if ':' not in host else f'[{host}]'}:{port}"}),
            _("Settings file: %(path)s") % {"path": settings.CONFIG_FILE},
        ]
        if not settings.SETUP_MODE:  # before installing there is no database yet: the installer asks
            lines += [
                _("Database: %(database)s") % {"database": database.where(settings.CONFIG["database"], settings.CONFIG_FILE)},
                _("Uploaded files: %(folder)s") % {"folder": settings.MEDIA_ROOT},
                _("Site: %(address)s") % {"address": f"{settings.SITE_URL or origin}/"},
            ]
        text = "\n".join(lines) + "\n" + banner.render(settings.SITE_URL or origin, self.style)
        if host in banner.WILDCARDS:
            local = "[::1]" if host == "::" else "127.0.0.1"
            if settings.SETUP_MODE:
                text += _("To open it from another computer, replace %(local)s with this machine's IP or domain name.") % {"local": local} + "\n"
            else:
                text += _("To open it from another computer, replace %(local)s with this machine's "
                          "IP or domain name (it must be in allowed_hosts in the settings file).") % {
                    "local": local} + "\n"
        if not settings.SETUP_MODE and (exposed := database.readable_sqlite_files(settings.CONFIG["database"], settings.CONFIG_FILE)):
            if os.name == "nt":  # the files SQLite makes next to it take their folder's access list
                folder = os.path.dirname(os.path.abspath(exposed[0]))
                text += self.style.WARNING(
                    _("Other users on this machine can read the database file %(path)s, or the files SQLite "
                      "writes next to it in that folder, which hold password hashes and the storage sources' "
                      "keys. Make the folder readable and writable by the user running the site only: %(command)s")
                    % {"path": exposed[0], "command": _private_folder_command(folder)}
                ) + "\n"
            else:
                text += self.style.WARNING(
                    _("Other users on this machine can read the database file %(path)s, "
                      "which holds password hashes and the storage sources' keys. Make it "
                      "readable and writable by the user running the site only: %(command)s")
                    % {"path": exposed[0], "command": "chmod 600 " + " ".join(shlex.quote(name) for name in exposed)}
                ) + "\n"
        if settings.DEBUG:
            text += self.style.WARNING(_("Debug mode is on (debug = true in the settings file); turn it off before going live.")) + "\n"
        if settings.SERVER["proxies"] and host in banner.WILDCARDS:
            text += self.style.WARNING(
                _("proxies = %(proxies)s is set (a reverse proxy is in front), but the server listens on all "
                  "addresses: anyone who reaches port %(port)s directly, around the proxy, can put anything "
                  "in X-Forwarded-For and pass for another IP. If the proxy is on this machine, set [server] "
                  "host to 127.0.0.1; otherwise let only the proxy reach this port with a firewall.")
                % {"proxies": settings.SERVER["proxies"], "port": port}
            ) + "\n"
        return text

    @staticmethod
    def listen_problem(host, port, error):
        where = f"{host}:{port}"
        code = getattr(error, "errno", None)
        # On Windows a socket's error may come as its WSA code (10048, 10013, 10049).
        if code in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)):
            return _("Port %(port)s is already used by another program (maybe another "
                     "HFile). Stop it first, or change port in [server] of the settings file.") % {"port": port}
        if code in (errno.EACCES, getattr(errno, "WSAEACCES", errno.EACCES)):
            if os.name == "nt":
                return _("No permission to listen on %(address)s: Windows keeps the port for something else "
                         "(netsh interface ipv4 show excludedportrange protocol=tcp lists such ports). "
                         "Use another port in [server] of the settings file.") % {"address": where}
            return _("No permission to listen on %(address)s: ports below 1024 usually need "
                     "administrator rights. Use a higher port, with a reverse proxy in front.") % {
                "address": where}
        if code in (errno.EADDRNOTAVAIL, getattr(errno, "WSAEADDRNOTAVAIL", errno.EADDRNOTAVAIL)) or isinstance(error, ValueError):
            return _("This machine has no address %(host)s. Change host in [server] of the settings "
                     'file, like "0.0.0.0" (all addresses) or "127.0.0.1" (this machine only).') % {
                "host": host}
        return _("Can't listen on %(address)s: %(error)s") % {"address": where, "error": error}
