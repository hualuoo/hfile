import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from apps.core.middleware import head_without_body, utf8_only  # noqa: E402  (the settings module is set above)

# Django reads a request's text in whatever charset its Content-Type names; utf8_only stops that
# before Django builds the request, where a middleware would be too late. Outside it, the answer to
# HEAD loses its body, which waitress would otherwise send.
application = head_without_body(utf8_only(get_wsgi_application()))

# Whatever server loads this, a new version upgrades the database (or checks that it needn't)
# before serving anything, and exits when it can't (settings.AUTO_UPGRADE, apps/upgrades/startup.py).
from apps.upgrades.startup import upgrade_on_start  # noqa: E402  (needs the settings loaded above)

upgrade_on_start()

# Then config.toml's admin address against the storage keys and the path-link prefix, which only
# the database knows (apps/siteconf/checks.py).
from apps.siteconf.checks import stop_on_conflict  # noqa: E402

stop_on_conflict()

# And from then on, a thread removes what has expired, every hour (apps/core/cleanup.py).
from apps.core import cleanup  # noqa: E402

cleanup.start()
