"""For admins: the 用户 and 登录日志 sections of /manage/.

    GET    manage/users/              ?search= (username, nickname or id) &is_active=true|false &kind= &ordering= &page=
    POST   manage/users/              {"username", "nickname", "password", "is_active", "default_permissions"}: 201
    GET    manage/users/{id}/
    PATCH  manage/users/{id}/         the same fields, each optional; "password": "" keeps the old one
    DELETE manage/users/{id}/         204
    POST   manage/users/{id}/copy/    {"username", "nickname", "password"}: 201, the copy

Every write answers with the user as GET shows them. Who may change whom is managing.py's; what a
user may do on each storage source (their grants) is the storages app's
(GET/PUT manage/users/{id}/grants/).

    GET    manage/login-logs/         ?username= &ip= &result= &since= &until= &page=, newest first
    DELETE manage/login-logs/         the same filters: deletes every entry they find (all of them with none)
    POST   manage/login-logs/delete/  {"ids": [...]}: deletes those entries
The two deletes answer {"deleted": n}.
"""

from datetime import datetime, time, timedelta

from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.filters import StableOrderingFilter
from apps.core.permissions import IsAdmin
from apps.core.query import QueryParamsMixin

from . import managing
from .manage_serializers import (
    CopyUserSerializer,
    LoginLogIdsSerializer,
    LoginLogQuery,
    LoginLogSerializer,
    ManagedUserSerializer,
    UserFormSerializer,
    UserListQuery,
)
from .models import LoginLog, User


class ManageUserViewSet(QueryParamsMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAdmin]
    serializer_class = ManagedUserSerializer
    filter_backends = [StableOrderingFilter]
    ordering_fields = ["id", "username", "date_joined", "last_login"]
    ordering = ["id"]
    lookup_value_regex = r"[0-9]+"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_query_serializer_class(self):
        return UserListQuery if self.action == "list" else None

    def get_queryset(self):
        users = User.objects.all()
        if self.action != "list":
            return users
        if text := self.query["search"]:
            match = Q(username__icontains=text) | Q(nickname__icontains=text)
            number = text.removeprefix("#")
            if number.isascii() and number.isdigit() and len(number) <= 18:
                match |= Q(pk=int(number))
            users = users.filter(match)
        if self.query["is_active"]:
            users = users.filter(is_active=self.query["is_active"] == "true")
        if self.query["kind"]:
            users = users.filter(kind=self.query["kind"])
        return users

    def get_serializer_context(self):
        # Whether a row is the last active superuser: counted once for the whole page.
        return {**super().get_serializer_context(), "admins": managing.admin_count()}

    def _answer(self, user, code=status.HTTP_200_OK):
        return Response(ManagedUserSerializer(user, context=self.get_serializer_context()).data, status=code)

    def create(self, request):
        form = UserFormSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        return self._answer(managing.create_user(request.user, form.validated_data), status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        target = self.get_object()
        form = UserFormSerializer(target, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        return self._answer(managing.update_user(request.user, target.pk, form.validated_data))

    def destroy(self, request, pk=None):
        managing.delete_user(request.user, self.get_object().pk)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def copy(self, request, pk=None):
        source = self.get_object()
        form = CopyUserSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        return self._answer(managing.copy_user(request.user, source.pk, form.validated_data), status.HTTP_201_CREATED)


def _start_of(day):
    """Midnight at the start of `day` in the site's time zone (TIME_ZONE)."""
    return timezone.make_aware(datetime.combine(day, time.min))


class ManageLoginLogView(QueryParamsMixin, ListAPIView):
    """The login log, newest first, with "newest" (the id of the newest entry the filters find); DELETE
    with the same filters (and ?newest=, what the list showed) deletes what they find."""

    serializer_class = LoginLogSerializer
    query_serializer_class = LoginLogQuery
    permission_classes = [IsAdmin]
    http_method_names = ["get", "delete", "head", "options"]

    def get_queryset(self):
        entries = LoginLog.objects.select_related("user").order_by("-created_at", "-id")
        query = self.query
        if query["username"]:
            entries = entries.filter(username__icontains=query["username"])
        if query["ip"]:
            entries = entries.filter(ip__startswith=query["ip"])
        if query["result"]:
            entries = entries.filter(result=query["result"])
        if query["since"]:
            entries = entries.filter(created_at__gte=_start_of(query["since"]))
        if query["until"]:
            entries = entries.filter(created_at__lt=_start_of(query["until"] + timedelta(days=1)))
        return entries

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response.data["newest"] = self.get_queryset().order_by().aggregate(newest=Max("id"))["newest"]
        return response

    def delete(self, request):
        query = self.strict_query()
        entries = self.get_queryset()
        if query["newest"]:
            entries = entries.filter(pk__lte=query["newest"])
        deleted, _ = entries.delete()
        return Response({"deleted": deleted})


class ManageLoginLogDeleteView(APIView):
    """POST {"ids": [...]}: deletes the entries ticked on the page (ids already gone are skipped)."""

    permission_classes = [IsAdmin]

    def post(self, request):
        form = LoginLogIdsSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        deleted, _ = LoginLog.objects.filter(pk__in=form.validated_data["ids"]).delete()
        return Response({"deleted": deleted})
