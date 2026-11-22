"""复制任务 on /manage/#copy-tasks, under /api/v1/. Admins only.

    GET    manage/copy-tasks/                 the tasks, newest first (pages of 20, ?page=): their settings, state
                                              and progress (service.summary)
    POST   manage/copy-tasks/                 {"source", "source_path", "target", "target_path", "threads",
                                              "speed_limit" (bytes a second, 0: none), "same_file", "same_name"
                                              ("skip" or "overwrite"), "retries"}: a new task, started at once (201);
                                              it copies the source folder into target_path as a folder of the same
                                              name, the task's target_path (service.destination())
    GET    manage/copy-tasks/{id}/            one task, with the files being copied ("current") and how many failed
    GET    manage/copy-tasks/{id}/failures/   the files and folders it couldn't copy (pages of 50)
    POST   manage/copy-tasks/{id}/stop/       stop it; what it copied stays (409 when it isn't running)
    POST   manage/copy-tasks/{id}/resume/     run it again when it failed or was stopped: the files in the target with
                                              the same name and size are skipped (409 otherwise)
    GET    manage/copy-tasks/storages/        the storage sources to copy from and into: [{"id", "name", "key",
                                              "type_label", "writable"}]
    GET    manage/copy-tasks/folders/         ?storage=&path=: the folders in a folder, for picking one
"""

from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import StandardPagination
from apps.core.permissions import IsAdmin

from . import service
from .models import CopyFailure, CopyTask
from .serializers import CreateSerializer, FoldersQuery


class _FailurePages(StandardPagination):
    page_size = 50
    max_page_size = 200


class CopyTasksView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        paginator = StandardPagination()
        tasks = CopyTask.objects.select_related("source", "target").order_by("-id")
        page = paginator.paginate_queryset(tasks, request, view=self)
        now = timezone.now()
        return paginator.get_paginated_response([service.summary(task, now) for task in page])

    def post(self, request):
        serializer = CreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = service.create(serializer.validated_data, request.user)
        task.refresh_from_db()
        return Response(service.detail(task, timezone.now()), status=status.HTTP_201_CREATED)


def _task(pk):
    task = CopyTask.objects.select_related("source", "target").filter(pk=pk).first()
    if task is None:
        raise NotFound(_("There is no such copy task."))
    return task


class CopyTaskView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, pk):
        return Response(service.detail(_task(pk), timezone.now()))


class CopyTaskFailuresView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, pk):
        task = _task(pk)
        paginator = _FailurePages()
        page = paginator.paginate_queryset(CopyFailure.objects.filter(task=task).order_by("id"), request, view=self)
        return paginator.get_paginated_response([
            {"id": failure.id, "path": failure.path, "is_dir": failure.is_dir, "size": failure.size,
             "message": failure.message, "attempts": failure.attempts}
            for failure in page])


class CopyTaskStopView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        task = service.stop(pk)
        return Response(service.detail(_task(task.pk), timezone.now()))


class CopyTaskResumeView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        task = service.resume(pk)
        return Response(service.detail(_task(task.pk), timezone.now()), status=status.HTTP_202_ACCEPTED)


class CopyStoragesView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(service.storage_choices())


class CopyFoldersView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        query = FoldersQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(service.folders(query.validated_data["storage"], query.validated_data["path"]))
