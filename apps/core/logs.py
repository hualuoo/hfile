"""Logging helpers for settings.LOGGING."""

import logging


class WithoutSecrets(logging.Filter):
    """The Django admin's random path and the secrets of links (apps/core/masking.py) left out of every
    console line: Django's own warnings name the address ("Forbidden (CSRF cookie not set.): /<admin>/login/").
    The traceback below a message too (logger.exception, Django's 500s): an exception may quote an
    address it was given. It is formatted here, masked, and kept as the record's exc_text, which the
    handler's formatter then writes instead of formatting it again."""

    def filter(self, record):
        from .masking import mask_text  # reads the settings, which aren't loaded at import

        try:
            message = record.getMessage()
        except Exception:  # a record whose arguments don't fit its message: leave it to the handler
            return True
        masked = mask_text(message)
        if masked != message:
            record.msg, record.args = masked, ()
        if record.exc_info and not record.exc_text:
            record.exc_text = _FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask_text(record.exc_text)
        if record.stack_info:
            record.stack_info = mask_text(record.stack_info)
        return True


_FORMATTER = logging.Formatter()  # formats a traceback as every formatter of settings.LOGGING does


class OneLine(logging.Filter):
    """Leaves the traceback out: for errors that are expected and say enough in a line, like a
    request for an address that isn't in allowed_hosts."""

    def filter(self, record):
        record.exc_info = None
        record.exc_text = None
        return True
