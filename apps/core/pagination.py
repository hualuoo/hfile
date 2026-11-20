from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination

from django.utils.translation import gettext as _


class _PageParams(serializers.Serializer):
    """?page= and ?page_size=, both whole numbers from 1; page_size at most the list's limit."""

    def __init__(self, *args, paginator, **kwargs):
        super().__init__(*args, **kwargs)
        page = _("The page number should be a positive integer.")
        size = _("The page size should be an integer from 1 to %(max_page_size)s.") % {"max_page_size": paginator.max_page_size}
        keys = ("invalid", "min_value", "max_value", "max_string_length")
        self.fields[paginator.page_query_param] = serializers.IntegerField(
            required=False, min_value=1, error_messages=dict.fromkeys(keys, page)
        )
        self.fields[paginator.page_size_query_param] = serializers.IntegerField(
            required=False, min_value=1, max_value=paginator.max_page_size, error_messages=dict.fromkeys(keys, size)
        )


class StandardPagination(PageNumberPagination):
    """?page= and ?page_size= are checked (a 400 when they aren't whole numbers in range;
    DRF's own pagination quietly uses the default size instead). A page after the last is 404."""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 50

    def paginate_queryset(self, queryset, request, view=None):
        params = _PageParams(data=request.query_params, paginator=self)
        params.is_valid(raise_exception=True)
        self.params = params.validated_data
        return super().paginate_queryset(queryset, request, view)

    def get_page_size(self, request):
        return getattr(self, "params", {}).get(self.page_size_query_param) or self.page_size

    def get_page_number(self, request, paginator):
        return getattr(self, "params", {}).get(self.page_query_param) or 1
