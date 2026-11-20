"""The command line of `hfile` (the single executable, built from hfile.py) and of manage.py: "cli.main".

    hfile [--config 路径] [命令 参数…]

Before Django starts: the single executable takes the time zones and CA certificates of the system
it runs on (frozen.py). Then this finds config.toml (configfile.locate: --config, else $HFILE_CONFIG,
else next to the program), creates it when it's missing, and checks it, so that a mistake in it is
a plain message instead of a traceback. Then runs the Django management command; `hfile` without
one runs `serve`, the site (apps/installer/management/commands/serve.py).

`serve` runs in a child process, started again whenever it exits with RESTART: that is how the
installer has the program read the settings it has just written (apps/installer/service.py
restart_soon), as runserver's autoreloader does for runserver.
"""

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time

from . import configfile, frozen
from .i18n import _
from .version import VERSION

RESTART = 3  # the exit status asking to be started again (the same as runserver's autoreloader)
CHILD = "HFILE_SERVE_CHILD"  # set for serve's child process
# How long the outer process waits for the server to stop by itself after a Ctrl+C: from a terminal
# the server got the SIGINT too. Then it gets a SIGTERM, which stops it when it didn't (`kill -INT`
# of the outer process only); one that is stopping already ignores it. On Windows that is TerminateProcess,
# which nothing ignores, and a Ctrl+C reaches both processes anyway (the same console): there it waits as
# long as the server gives the requests in hand (serve.py's STOP_WAIT), and a little longer.
SIGINT_GRACE = 35 if os.name == "nt" else 3
# _ctrl_c(): a SIGINT this soon after the one before is the same Ctrl+C, passed on (it came up to 18 ms later).
SAME_CTRL_C = 0.5

# What runs before HFile is installed (an empty secret_key): the servers, whose site is the
# installer, and commands that don't use the database.
SETUP_COMMANDS = {"serve", "runserver", "help", "check", "collectstatic", "version"}
# Asking for help doesn't create a settings file.
HELP = {None, "help", "--help", "-h", "version"}


def say(text):
    print(text, file=sys.stderr, flush=True)


def main(argv=None, default_command=None):
    """Returns the exit status."""
    child = os.environ.get(CHILD) == "1"
    _ctrl_c(passed_on=configfile.FROZEN and os.name != "nt" and not child)
    try:
        return _main(argv, default_command)
    except KeyboardInterrupt:
        if child:
            # The site, serve's child process: a Ctrl+C before it serves, while Django loads the apps (a second
            # or so in the single executable), or a second one while it stops, ends it as one while it serves
            # does (serve.py run()): with 0, not with a traceback.
            return 0
        # A command, or this process before it starts the site: a line instead of the traceback, in the system's
        # language (a command's ended with it: _plain_command_errors), and the exit status a shell gives a
        # program that Ctrl+C ended.
        from django.apps import apps

        if apps.ready:
            from django.utils import translation

            from .i18n import system_language

            translation.activate(system_language())
        say("\n" + _("Interrupted."))
        return 130


def _ctrl_c(passed_on):
    """Ctrl+C is KeyboardInterrupt, as Python has it, without two flaws.

    One that comes while Python runs a finalizer (__del__, a weakref's callback: the garbage collector runs
    them wherever the program is) can't be raised there: Python wrote "Exception ignored in ..." with a
    traceback and went on, and the Ctrl+C was lost (the site went on starting, and stopped on the outer
    process's SIGTERM 3 seconds later). It is raised again a moment later, without that message.

    `passed_on`: in the single executable outside Windows the process that was started is PyInstaller's
    bootloader, which runs this one as its child and passes on every signal it gets. A Ctrl+C in a terminal
    reaches both, so this process gets the SIGINT twice: in a command Python raised a second KeyboardInterrupt
    for 23 Ctrl+C of 60, up to 18 ms after the first. It is the same Ctrl+C; as another one it cut short what
    the first began (putting the database back as it was when an upgrade is stopped, until
    service.signals_held() holds them), or printed a traceback after main()'s line."""
    last = None  # when the last KeyboardInterrupt was raised

    def interrupted(signum, frame):
        nonlocal last
        now = time.monotonic()
        if last is not None and now - last < SAME_CTRL_C:
            return
        last = now
        raise KeyboardInterrupt

    def unraisable(info, default=sys.unraisablehook):
        nonlocal last
        if info.exc_type is not None and issubclass(info.exc_type, KeyboardInterrupt):
            last = None  # it wasn't raised: what is passed on of that Ctrl+C now counts
            # A moment later: raised now, it would come in here, and be lost as well.
            again = threading.Timer(0.05, _sigint_to_main_thread)
            again.daemon = True
            with contextlib.suppress(RuntimeError):  # no new thread once the program is ending, which it is then
                again.start()
            return
        default(info)

    if getattr(sys.unraisablehook, "keeps_ctrl_c", False):  # main() again, in the tests
        return
    if passed_on and signal.getsignal(signal.SIGINT) is signal.default_int_handler:  # not ignored (nohup)
        signal.signal(signal.SIGINT, interrupted)
    unraisable.keeps_ctrl_c = True
    sys.unraisablehook = unraisable


def _sigint_to_main_thread():
    """SIGINT for the main thread, where Python runs the signal handlers: sent to the process, another thread could
    get it, and the main one would go on waiting where it is (time.sleep(), a socket) until that ends."""
    if os.name == "nt":
        signal.raise_signal(signal.SIGINT)  # there Python's waits end on it, whichever thread raised it
    else:
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)


def _main(argv, default_command):
    _output_that_takes_anything()
    if configfile.FROZEN:
        frozen.use_system_files()  # time zones and CA certificates: this system's, not the builder's
    argv = sys.argv if argv is None else argv
    program = argv[0]
    try:
        given, args = configfile.split_argv(list(argv[1:]))
    except configfile.ConfigError as error:
        say(str(error))
        return 2
    if args in (["--version"], ["-V"]):  # the program's version; Django's own prints Django's
        import django

        print(f"HFile {VERSION} (Django {django.get_version()})")
        return 0
    try:
        if not args and default_command:
            args = [default_command]
        command = args[0] if args else None
        path = configfile.locate(given)
        if command not in HELP and configfile.create(path):
            say("\n".join([_("No settings file was found, so a new one was created: %(path)s") % {"path": path},
                           _("To change the port, the IP to listen on or the database, edit this file, then start the program again.")]))
        configfile.load(path)
    except configfile.ConfigError as error:
        say(str(error))
        return 1
    os.environ[configfile.ENV_VAR] = str(path)  # for settings.py, here and in what this starts
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    if command == "serve" and os.environ.get(CHILD) != "1":
        return supervise([*own_command(program), *args])
    if command == "runserver" and configfile.FROZEN and "--noreload" not in args:
        # Its autoreloader starts Python again with the script; a built program has neither.
        args.append("--noreload")

    from django.conf import settings

    from . import cli_help

    if settings.SETUP_MODE and command not in HELP and command not in SETUP_COMMANDS and not command.startswith("-"):
        say("\n".join([
            _("HFile isn't installed yet: the settings file %(path)s has no secret_key.") % {"path": path},
            _("Run hfile first (from source: python manage.py serve or runserver), "
              "open the address it prints in a browser and follow the setup wizard;\n"
              "or fill in [database] and [site] secret_key in the settings file yourself."),
        ]))
        return 1
    _plain_command_errors()
    cli_help.install()
    cli_help.Utility([program, *args]).execute()
    return 0


def _output_that_takes_anything():
    """stdout and stderr that write every message (OPS-03). A character their encoding doesn't have (cp1252 for
    a file on Windows, a service wrapper's) is written as \\uXXXX: stdout raised UnicodeEncodeError, and serve
    ended before it listened. When they aren't a terminal (a file, a pipe: systemd, docker), UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):  # none (a Windows service), or one put in its place
            continue
        if stream.isatty():
            stream.reconfigure(errors="backslashreplace")
        else:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def _plain_command_errors():
    """A command that fails with CommandError prints its message alone: Django puts the class name in
    front of it ("CommandError: 恢复失败：..."), an English word before every Chinese message of
    HFile's commands. The exit status stays the error's returncode; --traceback still shows it all.

    And a command speaks the system's language (i18n.py), from the command line only, its help too (--help,
    which argparse writes before the command runs, and `help <command>`; argparse's and Django's texts in it,
    cli_help.py): call_command() keeps the one that is active."""
    from django.core.management.base import BaseCommand, CommandError, SystemCheckError
    from django.utils import translation

    from .i18n import system_language

    run_from_argv = BaseCommand.run_from_argv
    print_help = BaseCommand.print_help
    if getattr(run_from_argv, "plain_errors", False):
        return

    def run(self, argv):
        execute = self.execute

        def execute_plainly(*args, **options):
            try:
                return execute(*args, **options)
            except CommandError as error:
                if options.get("traceback") or isinstance(error, SystemCheckError):
                    raise  # Django's own handling
                self.stderr.write(str(error))
                sys.exit(error.returncode)

        self.execute = execute_plainly
        with translation.override(system_language()):
            return run_from_argv(self, argv)

    def help_in_its_language(self, prog_name, subcommand):
        with translation.override(system_language()):
            return print_help(self, prog_name, subcommand)

    run.plain_errors = True
    BaseCommand.run_from_argv = run
    BaseCommand.print_help = help_in_its_language


def own_command(program):
    """How to start this program again: the executable itself when built as one, else Python with the script."""
    executable = configfile.executable()
    return [executable] if executable else [sys.executable, os.path.abspath(program)]


def supervise(command):
    """Runs `command` (the server) as a child process, again whenever it exits with RESTART.

    This outer process does nothing else. Ctrl+C reaches the child as well (the same console);
    SIGTERM (systemd, docker stop) is passed on to it, and SIGINT_GRACE seconds after a SIGINT it gets
    a SIGTERM too, for a SIGINT that only this process got (kill -INT). Either way the child stops,
    and then this returns its exit status. On Linux the child also stops if this process is killed
    (PR_SET_PDEATHSIG), and in the single executable this process stops if the bootloader is
    (_end_with_bootloader). On Windows a job object does that (config/windows.py), and Ctrl+Break
    is taken as Ctrl+C: both reach the child, which stops on either (serve.py).
    """
    env = {**os.environ, CHILD: "1"}
    child = None
    stopping = False
    extra = {"preexec_fn": _stop_with(os.getpid())} if sys.platform.startswith("linux") else {}
    if os.name == "nt":
        from . import windows

        windows.end_children_with_this_process()

    def pass_on(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    previous = signal.signal(signal.SIGTERM, pass_on)
    previous_break = signal.signal(signal.SIGBREAK, signal.default_int_handler) if os.name == "nt" else None
    try:
        if not _end_with_bootloader():  # it was killed before this could start anything
            return 1
        while True:
            child = subprocess.Popen(command, env=env, **extra)
            if stopping:  # SIGTERM came while it was being started, between two runs
                child.terminate()
            while True:
                try:
                    code = _wait(child)
                    break
                except KeyboardInterrupt:  # the child got it too, from the console: wait for it to stop
                    stopping = True
                    if not _stops_by_itself(child):  # it didn't: the signal was for this process alone
                        _interrupt(child)
            if code != RESTART or stopping:
                return 128 - code if code < 0 else code  # killed by a signal: as a shell says it
            say("\n" + _("Restarting with the new settings…") + "\n")
    except KeyboardInterrupt:  # Ctrl+C between two runs
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
        if previous_break is not None:
            signal.signal(signal.SIGBREAK, previous_break)


def _stops_by_itself(child, seconds=SIGINT_GRACE):
    try:
        _wait(child, seconds)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        return False
    return True


def _wait(child, timeout=None):
    """child.wait(timeout), but not on Popen.wait()'s lock outside Windows: a KeyboardInterrupt just after
    it takes that lock, before the try that lets go of it, leaves it held, and the next wait() waits for it
    forever. In the single executable a Ctrl+C comes twice, from the terminal and passed on by PyInstaller's
    bootloader, and now and then the second one came just there: the program didn't stop.

    As Popen.wait() does, a Ctrl+C while it waits without a timeout first gives the child, which most likely
    got it too, a quarter of a second to end. That second SIGINT comes in the meantime and goes with the
    first; later, in _stops_by_itself(), it would count as another Ctrl+C."""
    if os.name == "nt":
        return child.wait(timeout)
    try:
        return _reap(child, timeout)
    except KeyboardInterrupt:
        if timeout is None:
            with contextlib.suppress(subprocess.TimeoutExpired, KeyboardInterrupt):
                _reap(child, 0.25)
        raise


def _reap(child, timeout):
    """The child's exit status, with os.waitpid(); TimeoutExpired after `timeout` seconds unless None."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while child.returncode is None:
        try:
            pid, status = os.waitpid(child.pid, 0 if deadline is None else os.WNOHANG)
        except ChildProcessError:  # child.poll() (pass_on) has taken its status
            if child.returncode is None:
                child.returncode = 0  # lost: what subprocess says then too
            break
        if pid == child.pid:
            child.returncode = os.waitstatus_to_exitcode(status)
        elif deadline is not None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(child.args, timeout)
            time.sleep(0.05)
    return child.returncode


def _interrupt(child):
    if child.poll() is not None:
        return
    # SIGTERM (Windows: TerminateProcess). A server that got the Ctrl+C as well is stopping already and
    # ignores it (serve.py's run()): its requests in hand still finish. A SIGINT cut them short (OPS-01).
    child.terminate()


def _stop_with(parent):
    """For the child, on Linux: SIGTERM when `parent` (this process) ends, even by SIGKILL, instead of
    serving on alone and holding the port."""

    def arrange():
        if _on_parents_death(signal.SIGTERM) and os.getppid() != parent:  # gone before that took effect
            os._exit(1)

    return arrange


def _end_with_bootloader():
    """The single executable on Linux: the process that was started is PyInstaller's bootloader, which
    runs this one as its child and waits for it. Killed (kill -9) it can pass nothing on, and this
    process and the server would go on holding the port. So this one gets SIGTERM when it ends, and
    stops the server as for any SIGTERM. False when the bootloader was gone before that took effect.
    On Windows this process ends with it, and the server with this one (windows.end_with_parent)."""
    if configfile.FROZEN and os.name == "nt":
        from . import windows

        return windows.end_with_parent(sys.executable)
    if not (configfile.FROZEN and sys.platform.startswith("linux")):
        return True
    parent = os.getppid()
    try:  # only that parent: not a shell or systemd, when a build runs the program without a bootloader
        ours = os.path.realpath(f"/proc/{parent}/exe") == os.path.realpath(sys.executable)
    except OSError:
        return True
    if not ours or not _on_parents_death(signal.SIGTERM):
        return True
    return os.getppid() == parent


def _on_parents_death(signum):
    """Linux's prctl(PR_SET_PDEATHSIG): `signum` for this process when its parent ends. Whether it took."""
    try:
        import ctypes

        return ctypes.CDLL(None, use_errno=True).prctl(1, signum) == 0  # 1 = PR_SET_PDEATHSIG
    except Exception:
        return False
