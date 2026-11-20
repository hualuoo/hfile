"""Python remembers every codec name it was asked for and didn't find, for as long as the process runs:
the codec registry's search function, encodings.search_function, stores None in its dict _cache for
the name (CPython's Lib/encodings/__init__.py), and nothing ever takes it out.

Clients choose such names before anybody has logged in: the charset of Content-Type
(apps.core.middleware.utf8_only, then Django), RFC 2231 parameters (x*=NAME''…) in Content-Type, in
Accept (DRF reads it on every API request) and in the part headers of a multipart body (Django's
MultiPartParser, already for the CSRF check of a form); storage servers too, with the encoding an
XML answer declares (expat looks it up). Every new name stayed in memory until a restart, up to
10 000 characters each (Django's MAX_HEADER_LENGTH), as often as a client liked.

keep_found_only() replaces that dict with one that keeps the codecs found and drops the None of a
name not found (and the names not found so far). config/settings.py calls it: serve, runserver, every
manage.py command, the tests and the single executable load the settings before they read any request.
Why that is safe, process-wide:

* The registry itself isn't touched: encodings.search_function stays where it is, first among the
  search functions. (Unregistering it and registering a wrapper would put it after the ones registered
  since: on Windows encodings._alias_mbcs would then answer the ANSI code page's name, cp1252 or
  cp936, with the "mbcs" codec instead of Python's own.)
* Every name gets the same answer as before. A codec found is kept, here and in the registry's own
  cache in C, which only ever keeps codecs found. A name not found is looked for again when it is
  asked for again, and not found again: one import in the encodings package that fails (and keeps
  nothing either). A new name cost that already, so a client gains nothing by repeating one.
* The name itself goes too: the registry interns the name it passes to the search function, and an
  interned string is freed with its last reference (CPython 3.12; config's tests check it), which was
  the key in _cache.
* The dict is swapped once, at startup, before any request; storing into it is still a single dict
  store, as atomic as before.

On a Python whose encodings module has no such dict it does nothing, and config's tests fail, so that
someone looks at what that Python keeps instead.
"""

import encodings


class FoundOnly(dict):
    """encodings._cache that leaves out the None stored for a name not found."""

    def __setitem__(self, name, entry):
        if entry is not None:
            dict.__setitem__(self, name, entry)


def keep_found_only(module=encodings):
    """Has `module` (the encodings package) keep the codecs it finds and none of the names it doesn't.
    Returns whether it does: False when its _cache isn't the plain dict this expects. Idempotent."""
    cache = getattr(module, "_cache", None)
    if isinstance(cache, FoundOnly):
        return True
    if type(cache) is not dict:
        return False
    module._cache = FoundOnly((name, entry) for name, entry in cache.items() if entry is not None)
    return True
