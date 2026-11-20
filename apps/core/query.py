"""Query-string parameters (?search=, ?status=, ...), checked by serializers like request bodies.

A list view describes its parameters with a serializer (query_serializer_class, or
get_query_serializer_class() when actions differ) and reads the checked values from
self.query. A value that breaks the rules answers 400 saying which parameter and why,
as a request body would; leaving a parameter out, or sending it empty (?status=), means
"no filter". Parameters a view doesn't know are ignored, as browsers and caches add some, except
where the filters say what to delete (strict_query()).

?page= and ?page_size= are checked by apps.core.pagination for every list, ?ordering= and
?search= by apps.core.filters.
"""

import re
from functools import cached_property

from rest_framework import exceptions, serializers

from django.utils.translation import gettext as _, ngettext

from .fields import StringField

SEARCH_MAX = 100  # characters in ?search= and the other text filters
ID_MAX = 2**63 - 1  # the id columns are BIGINT (settings.DEFAULT_AUTO_FIELD)
USERNAME_MAX = 150  # AbstractUser.username; names made in the Django admin may be longer than registering allows


class TextParam(StringField):
    """Free text such as ?search=: spaces at either end dropped, "" when left out."""

    def __init__(self, max_length=SEARCH_MAX, **kwargs):
        kwargs.setdefault("error_messages", {"max_length": ngettext("At most %(max_length)s character.", "At most %(max_length)s characters.", max_length) % {"max_length": max_length}})
        super().__init__(required=False, allow_blank=True, default="", max_length=max_length, **kwargs)


class IdParam(serializers.IntegerField):
    """The id of a user, a storage, ...: a whole number from 1. None when left out."""

    def __init__(self, what, **kwargs):
        message = _("Should be a %(what)s id (a positive integer).") % {"what": what}
        errors = {"invalid": message, "min_value": message, "max_value": message, "max_string_length": message}
        super().__init__(required=False, default=None, min_value=1, max_value=ID_MAX, error_messages=errors, **kwargs)


class ChoiceParam(serializers.ChoiceField):
    """One of `choices` (values, or (value, label) pairs). "" when left out, unless there is a `default`.

    Anything else is a 400 listing the values, or saying `message` when the list is too long to show.
    """

    def __init__(self, choices, *, default="", message=None, **kwargs):
        super().__init__(choices, required=False, allow_blank=True, default=default, **kwargs)
        self.error_messages["invalid_choice"] = message or _("Options: %(options)s.") % {"options": _(", ").join(map(str, self.choices))}

    def to_internal_value(self, data):
        if data == "" and self.default != "":
            return self.default  # ?status= is the same as leaving it out
        return super().to_internal_value(data)


# Dates far outside the site's lifetime would overflow when turned into times in UTC.
_YEARS = range(1970, 9999)


_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class DateParam(serializers.DateField):
    """A day as YYYY-MM-DD, exactly: Django's ISO parser also takes 20261101, 2026-W44-1 (a week day),
    2026-1-1 and full-width digits. None when left out."""

    def __init__(self, **kwargs):
        errors = {"invalid": _("Dates should be written as YYYY-MM-DD."), "datetime": _("Dates should be written as YYYY-MM-DD.")}
        super().__init__(required=False, default=None, input_formats=["iso-8601"], error_messages=errors, **kwargs)

    def to_internal_value(self, value):
        if isinstance(value, str) and not _DAY.fullmatch(value):
            self.fail("invalid")
        day = super().to_internal_value(value)
        if day.year not in _YEARS:
            raise serializers.ValidationError(_("The date is out of range."))
        return day


def check_query(serializer_class, request, context=None):
    """request.query_params checked by `serializer_class`: its validated_data, or a 400."""
    serializer = serializer_class(data=request.query_params, context=context or {"request": request})
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _has_body(request) -> bool:
    meta = request.META
    if meta.get("HTTP_TRANSFER_ENCODING"):
        return True
    try:
        return int(meta.get("CONTENT_LENGTH") or 0) > 0
    except ValueError:
        return True


class QueryParamsMixin:
    """For API views: self.query, the query parameters checked by the view's query serializer."""

    query_serializer_class = None

    def get_query_serializer_class(self):
        return self.query_serializer_class

    def strict_query(self):
        """self.query for a request that deletes what the filters find (DELETE on a log). There a filter
        that isn't read widens the delete, up to every row (API-02): a parameter the view doesn't know
        (a typo, since[0], Since) is a 400 instead of being ignored, and so is a body (conditions put
        there instead of the query string are never read)."""
        known = set(self.get_query_serializer_class()().fields)
        unknown = sorted(name for name in self.request.query_params if name not in known)
        if unknown:
            raise exceptions.ValidationError(
                {name[:100]: [_("Unknown filter. When deleting by filters, an unknown "
                                "one can't be ignored (that would delete more).")] for name in unknown[:20]})
        if _has_body(self.request):
            raise exceptions.ValidationError({"detail": _("When deleting by filters, put them in the address's query parameters; a request body isn't accepted.")})
        return self.query

    @cached_property  # a view instance serves one request
    def query(self):
        serializer_class = self.get_query_serializer_class()
        if serializer_class is None:
            return {}
        if hasattr(self, "get_serializer_context"):
            context = self.get_serializer_context()
        else:
            context = {"request": self.request, "view": self}
        return check_query(serializer_class, self.request, context)
