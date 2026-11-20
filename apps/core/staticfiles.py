"""Static file URLs that carry a fingerprint of the file: /static/web/js/common.js?v=3f9a0c1b2d4e.

A changed file has a new URL, so it is fetched at once, also after the program is updated. Set as
the "staticfiles" storage in config/settings.py; {% static %} and static() use it.

StaticFilesMiddleware serves them, with DEBUG off too: the single executable has no web server in
front that would. How long a browser keeps a file:
- with the file's own fingerprint: a year, without asking again (immutable). That URL never
  shows anything else, so a page needs only the requests for what changed;
- with another fingerprint (a page from before the file changed): not kept, it's the file as it
  is now under an old URL;
- without one (the fonts the CSS names, the Django admin's pictures): a day, or not at all when
  developing. One that changes keeps its old copy for that long.
"""

import hashlib
import os

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import StaticFilesStorage, staticfiles_storage
from whitenoise.middleware import WhiteNoiseMiddleware

FOREVER = "public, max-age=31536000, immutable"  # a year, the longest browsers keep anything
UNVERSIONED = "public, max-age=86400"

# name -> (path, mtime_ns, size, fingerprint): a file is read again only when it changes.
_fingerprints = {}
_sources = {}  # name -> the file served for it (not while developing, when files come and go)


class VersionedStaticFilesStorage(StaticFilesStorage):
    def url(self, name):
        url = super().url(name)
        fingerprint = self.fingerprint(name)
        return f"{url}?v={fingerprint}" if fingerprint else url

    def source(self, name):
        """The file the URL serves: the apps' own one, which StaticFilesMiddleware (WhiteNoise with
        WHITENOISE_USE_FINDERS) and runserver serve over a collected copy in STATIC_ROOT; that copy
        only when no app has the file. Fingerprinting a stale copy instead would give the new file
        the old URL, kept by browsers for a year."""
        if name in _sources:
            return _sources[name]
        found = finders.find(name) or (self.path(name) if self.exists(name) else None)
        if not settings.DEBUG:
            _sources[name] = found
        return found

    def fingerprint(self, name):
        path = self.source(name)
        if not path:
            return ""
        try:
            stat = os.stat(path)
        except OSError:
            return ""
        cached = _fingerprints.get(name)
        if cached and cached[:3] == (path, stat.st_mtime_ns, stat.st_size):
            return cached[3]
        digest = hashlib.blake2b(digest_size=6)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                digest.update(chunk)
        _fingerprints[name] = (path, stat.st_mtime_ns, stat.st_size, digest.hexdigest())
        return _fingerprints[name][3]


class StaticFilesMiddleware(WhiteNoiseMiddleware):
    """WhiteNoise, which answers /static/ before the rest of the site. With WHITENOISE_USE_FINDERS it
    serves the apps' own files, so there is no need for collectstatic; STATIC_ROOT is used only when
    it exists (WhiteNoise would warn about it on every start)."""

    def add_files(self, root, prefix=None):
        if self.autorefresh or os.path.isdir(root):
            super().add_files(root, prefix)

    def serve(self, static_file, request):
        """WhiteNoise's answer, kept as long as the top of this file says."""
        response = super().serve(static_file, request)
        if response.status_code not in (200, 206, 304):
            return response
        version = request.GET.get("v")
        if version is not None:
            fingerprint = staticfiles_storage.fingerprint(request.path_info[len(self.static_prefix) :])
            response["Cache-Control"] = FOREVER if version == fingerprint else "no-cache"
        elif not settings.DEBUG:
            response["Cache-Control"] = UNVERSIONED
        return response
