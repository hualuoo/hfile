"""Files the site saved in [paths] uploads (settings.MEDIA_ROOT), served at /media/<name> (config/urls.py).

Only names the site gave them are served. Each app registers the names it gives, from its
AppConfig.ready() (siteconf registers site/<32 hex digits>.<ext>, the logo and icon):

    uploads.register(r"site/[0-9a-f]{32}\\.(?:png|ico|webp|jpe?g|gif)")

Anything else in the folder is a 404, also when [paths] uploads is set to the folder of
config.toml, the database or the backups; and only once links are followed and the file is still
inside the folder. Every saved file gets a new random name, so a URL never shows another file:
browsers keep it a year. Only browsers: a shared cache (a CDN, a caching proxy) would go on handing
out a file an admin has deleted.
"""

import os
import re
import threading

from django.conf import settings
from django.http import Http404
from django.utils.translation import gettext as _
from django.views.static import serve as static_serve

_lock = threading.Lock()
_patterns = []


def register(pattern):
    """Serves the names that `pattern` (a regex, matched against the whole relative name) matches."""
    compiled = re.compile(pattern, re.ASCII)
    with _lock:
        if compiled.pattern not in [p.pattern for p in _patterns]:
            _patterns.append(compiled)


def allowed(name):
    return any(pattern.fullmatch(name) for pattern in list(_patterns))


def serve(request, path):
    """An uploaded file, from MEDIA_ROOT as it is now (tests change it)."""
    root = os.path.realpath(settings.MEDIA_ROOT)
    if not allowed(path) or not os.path.realpath(os.path.join(root, path)).startswith(root + os.sep):
        raise Http404(_("No such file."))
    response = static_serve(request, path, document_root=settings.MEDIA_ROOT)
    response["Cache-Control"] = "private, max-age=31536000, immutable"
    return response
