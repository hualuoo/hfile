"""HFile's version.

Raised for every release, which is the tag v<VERSION>: .github/workflows/release.yml builds nothing when the
two differ (docs/build.md). The upgrade log (upgrades app) records which version upgraded the database and when;
a new version without new migrations records a row with none. /manage/#version and `hfile --version` show it.
Whether the database needs upgrading does not depend on it: that comes from the migrations
(apps/upgrades/service.py).
"""

VERSION = "1.2.2"
