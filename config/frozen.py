"""What the single executable (configfile.FROZEN) takes from the system it runs on: time zones and
CA certificates. config/cli.py calls use_system_files() before anything else.

PyInstaller copies Python and OpenSSL as they are on the machine that builds the program, and
the paths of that machine stay in them: conda's Python looks for time zones only in its own
share/zoneinfo, OpenSSL for CA certificates only in its own ssl/ folder. A server has neither
folder. There every page with a time on it failed (ZoneInfoNotFoundError: 'No time zone found
with key Asia/Shanghai'), and mail over SSL/TLS or STARTTLS couldn't check the mail server's
certificate.

So the program looks where Linux keeps them, like any other Python, and on a system that has
none (a minimal container image) it uses its own copies: the tzdata and certifi packages, built
in (scripts/build.sh); the C library gets tzdata's through TZDIR. PYTHONTZPATH, TZDIR,
SSL_CERT_FILE or SSL_CERT_DIR, when set, are used as they are.
"""

import os
import zoneinfo

# CPython's own default (./configure --with-tzpath). After these zoneinfo tries tzdata.
TZPATH = ("/usr/share/zoneinfo", "/usr/lib/zoneinfo", "/usr/share/lib/zoneinfo", "/etc/zoneinfo")
# Where Linux distributions keep all the CA certificates in one file (the list Go uses).
CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Gentoo
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL, CentOS, Rocky, Alma
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/pki/tls/cacert.pem",  # OpenELEC
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL 7 and later
    "/etc/ssl/cert.pem",  # Alpine
)
# The SSL_CERT_FILE and TZDIR chosen here. The program that this one starts (serve's child,
# config/cli.py) chooses for itself: certifi's and tzdata's files are in the folder each run of the
# program unpacks itself to.
CHOSEN = "HFILE_CHOSEN_CA_FILE"
CHOSEN_TZDIR = "HFILE_CHOSEN_TZDIR"


def use_system_files(environ=os.environ):
    if "PYTHONTZPATH" not in environ:
        # Windows has no such folders: there, as for any Python, the time zones are tzdata's.
        zoneinfo.reset_tzpath(TZPATH if os.name == "posix" else ())
    if os.name != "posix":  # Windows: Python reads the system's certificate store itself
        return
    if environ.get("SSL_CERT_FILE") and environ.get("SSL_CERT_FILE") == environ.get(CHOSEN):
        del environ["SSL_CERT_FILE"]
    if not environ.get("SSL_CERT_FILE") and not environ.get("SSL_CERT_DIR"):
        environ["SSL_CERT_FILE"] = environ[CHOSEN] = ca_file()
    # A system without time zones (a minimal container image): Python reads tzdata's, and so does the
    # C library, for the times in the log (Django sets TZ to TIME_ZONE), through TZDIR.
    if environ.get("TZDIR") and environ.get("TZDIR") == environ.get(CHOSEN_TZDIR):
        del environ["TZDIR"]
    if not environ.get("TZDIR") and not any(os.path.isdir(path) for path in TZPATH) and (folder := tzdata_folder()):
        environ["TZDIR"] = environ[CHOSEN_TZDIR] = folder


def ca_file():
    """The system's CA certificates, or else certifi's."""
    for path in CA_FILES:
        if os.path.isfile(path):
            return path
    import certifi

    return certifi.where()


def tzdata_folder():
    """The zoneinfo folder of the tzdata package (the program's own copy), or "" without one."""
    try:
        import tzdata
    except ImportError:
        return ""
    folder = os.path.join(os.path.dirname(tzdata.__file__), "zoneinfo")
    return folder if os.path.isfile(os.path.join(folder, "Asia", "Shanghai")) else ""
