"""Cross-checks of the access rules before the release: tests only (apps/crosscheck/tests/), no models.

Each rule (the actor's root, filter rules, folder passwords, signed links, inline answers, the local
driver's symlinks) is tried through every endpoint that takes a storage and a path, for the guest, a
normal user, a share's visitor and a staff admin. Run them with `manage.py test apps.crosscheck`.
"""
