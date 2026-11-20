import operator
from functools import reduce

from django.db.models import Exists, OuterRef, Q
from django.utils.translation import gettext as _, ngettext
from rest_framework import serializers
from rest_framework.filters import OrderingFilter
from rest_framework.filters import SearchFilter as DRFSearchFilter

from .fields import StringField
from .query import TextParam, check_query

ORDERING_MAX = 200  # characters in ?ordering=
SEARCH_TERMS_MAX = 10  # words in ?search=: each one may be a join of its own (many-to-many fields)


class _SearchParams(serializers.Serializer):
    search = TextParam()  # DRF's SEARCH_PARAM


class SearchFilter(DRFSearchFilter):
    """DRF's ?search=, checked first (apps.core.query.TextParam): at most SEARCH_MAX characters and
    SEARCH_TERMS_MAX words. A row matches when every word is in one of its search_fields."""

    def get_search_terms(self, request):
        check_query(_SearchParams, request)
        terms = super().get_search_terms(request)
        if len(terms) > SEARCH_TERMS_MAX:
            raise serializers.ValidationError({self.search_param: [ngettext("At most %(limit)s word.", "At most %(limit)s words.", SEARCH_TERMS_MAX) % {"limit": SEARCH_TERMS_MAX}]})
        return terms

    def filter_queryset(self, request, queryset, view):
        search_fields = self.get_search_fields(view, request)
        search_terms = self.get_search_terms(request)
        if not search_fields or not search_terms:
            return queryset
        lookups = [self.construct_search(str(field), queryset) for field in search_fields]
        base = queryset
        # One filter() per word, so that two words can match two different related rows (two tags). DRF puts them all
        # in one, and a many-to-many join then needs one of them to match every word.
        for term in search_terms:
            queryset = queryset.filter(reduce(operator.or_, (Q(**{lookup: term}) for lookup in lookups)))
        if self.must_call_distinct(queryset, search_fields):
            # The joins can match a row several times; Exists keeps each once (as DRF does).
            queryset = base.filter(Exists(queryset.filter(pk=OuterRef("pk"))))
        return queryset


class OrderingParam(StringField):
    """?ordering=: names from `allowed`, each with "-" in front for descending, separated by commas."""

    def __init__(self, allowed, **kwargs):
        self.allowed = list(allowed)
        super().__init__(required=False, allow_blank=True, default="", max_length=ORDERING_MAX, **kwargs)

    def to_internal_value(self, data):
        terms = [term.strip() for term in super().to_internal_value(data).split(",") if term.strip()]
        for term in terms:
            if term.removeprefix("-") not in self.allowed:
                options = _(", ").join(self.allowed)
                raise serializers.ValidationError(_("Can't sort by \"%(term)s\". Options: %(options)s (a leading - sorts from largest to smallest).") % {"term": term, "options": options})
        return terms


class _OrderingParams(serializers.Serializer):
    def __init__(self, *args, param, allowed, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[param] = OrderingParam(allowed)


class StableOrderingFilter(OrderingFilter):
    """?ordering= by the view's ordering_fields only: anything else is a 400 that lists them
    (DRF's own filter quietly drops unknown names). The ORDER BY always ends with -id, so
    ties cannot shuffle between pages."""

    def get_ordering(self, request, queryset, view):
        allowed = [name for name, _label in self.get_valid_fields(queryset, view, {"request": request})]
        params = _OrderingParams(data=request.query_params, param=self.ordering_param, allowed=allowed)
        params.is_valid(raise_exception=True)
        ordering = params.validated_data[self.ordering_param] or list(self.get_default_ordering(view) or [])
        if not any(f.lstrip("-") in ("id", "pk") for f in ordering):
            ordering.append("-id")
        return ordering
