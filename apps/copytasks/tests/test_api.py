"""The copy task API (apps/copytasks/views.py): admins only, what it checks before a task is made, and what the page
gets back."""

from unittest import mock

from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.activity.describe import describe
from apps.activity.models import ActivityLog

from .. import runner
from ..models import CopyFailure, CopyTask
from .test_runner import CopyTestCase, options

API = "/api/v1/manage/copy-tasks/"
Status = CopyTask.Status


class ApiTestCase(CopyTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient(enforce_csrf_checks=True)
        self.login(self.admin)

    def login(self, user):
        self.client.force_login(user)
        response = self.client.get("/api/v1/session/")
        self.client.credentials(HTTP_X_CSRFTOKEN=response.json()["csrf_token"])

    def create(self, **changes):
        data = options(source=self.source.pk, target=self.target.pk)
        data.update(changes)
        return self.client.post(API, data, format="json")


class PermissionTests(ApiTestCase):
    def test_admins_only(self):
        with mock.patch.object(runner, "start_thread"):
            task_id = self.create().json()["id"]
        paths = [("get", API), ("post", API), ("get", f"{API}{task_id}/"), ("get", f"{API}{task_id}/failures/"),
                 ("post", f"{API}{task_id}/stop/"), ("post", f"{API}{task_id}/resume/"), ("get", f"{API}storages/"),
                 ("get", f"{API}folders/?storage={self.source.pk}")]
        member = User.objects.create_user("bob", password="pw-bob-2026")
        for user in (None, member):
            client = APIClient()
            if user is not None:
                client.force_login(user)
            for method, path in paths:
                with self.subTest(user=user, method=method, path=path):
                    response = getattr(client, method)(path, {}, format="json")
                    self.assertIn(response.status_code, (401, 403))
        self.assertEqual(CopyTask.objects.count(), 1)


class CreateTests(ApiTestCase):
    def test_creates_and_runs_it(self):
        response = self.create(threads=3, speed_limit=1024 * 1024, same_name="skip", retries=0)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["status"], Status.DONE)
        self.assertEqual(data["files"], {"total": 4, "copied": 4, "skipped": 0, "failed": 0})
        self.assertEqual((data["threads"], data["speed_limit"], data["same_file"], data["same_name"], data["retries"]),
                         (3, 1024 * 1024, "skip", "skip", 0))
        self.assertEqual(data["source"], {"id": self.source.pk, "name": "存储 src", "key": "src"})
        self.assertEqual(data["created_by"], {"id": self.admin.pk, "username": "admin"})
        self.assertEqual((data["can_stop"], data["can_resume"], data["current"], data["failures_count"]), (False, False, [], 0))
        entry = ActivityLog.objects.get(kind=ActivityLog.Kind.CREATE_COPY_TASK)
        self.assertEqual((entry.actor, entry.storage, entry.path, entry.to_path), (self.admin, self.source, "/", "/"))
        self.assertEqual(entry.detail, {"task": data["id"], "to_storage": "存储 dst", "to_storage_id": self.target.pk,
                                        "threads": 3, "speed_limit": 1024 * 1024, "same_file": "skip",
                                        "same_name": "skip", "retries": 0})
        self.assertEqual(describe(entry), f"任务 #{data['id']}；→ 存储 dst：/；3 个线程，限速 1.0\xa0MB/秒，"
                                          "相同文件：跳过，同名文件：跳过，重试 0 次")

    def test_defaults(self):
        with mock.patch.object(runner, "start_thread"):
            response = self.client.post(API, {"source": self.source.pk, "target": self.target.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual((data["source_path"], data["target_path"], data["threads"], data["speed_limit"],
                          data["same_file"], data["same_name"], data["retries"]), ("/", "/", 4, 0, "skip", "overwrite", 3))
        self.assertEqual((data["status"], data["scanning"], data["can_stop"]), (Status.RUNNING, True, True))

    def test_refused(self):
        disabled = self.storage("off", "local", self.folder("off"), enabled=False)
        readonly = self.storage("ro", "test-copy-readonly", self.folder("ro"))
        self.files(self.src, {"file.txt": b"x"})
        cases = [
            ({"source": 999}, "source", "没有这个存储源。"),
            ({"source": "x"}, "source", "请选择从哪个存储源复制。"),
            ({"target": None}, "target", "请选择复制到哪个存储源。"),
            ({"source": disabled.pk}, "source", "这个存储源已停用，请先启用它。"),
            ({"target": readonly.pk}, "target", "不能复制到这个存储源：它是只读的，或者不能上传文件。"),
            ({"source_path": "/a/../b"}, "source_path", "路径里不能有 . 或 .. 这样只有点和空格的一段。"),
            ({"source_path": "/missing"}, "source_path", "这个文件夹不存在。"),
            ({"source_path": "/file.txt"}, "source_path", "这是一个文件，不是文件夹。"),
            ({"target_path": "/x/" * 300}, "target_path", "路径的层数太多（最多 256 层）。"),
            ({"threads": 0}, "threads", "线程数：1 到 16 的整数。"),
            ({"threads": 17}, "threads", "线程数：1 到 16 的整数。"),
            ({"threads": True}, "threads", "线程数：1 到 16 的整数。"),
            ({"speed_limit": -1}, "speed_limit", "限速的单位是字节每秒：0（不限）到 10.0\xa0GB 的整数。"),
            ({"retries": 11}, "retries", "重试次数：0 到 10 的整数。"),
            ({"same_file": "rename"}, "same_file", "只能是 skip（跳过）或 overwrite（覆盖）。"),
        ]
        for changes, field, message in cases:
            with self.subTest(changes=changes):
                response = self.create(**changes)
                self.assertEqual(response.status_code, 400, response.content)
                if message:
                    self.assertEqual(response.json(), {field: [message]})
        self.assertEqual(CopyTask.objects.count(), 0)

    def test_folders_of_one_storage_apart(self):
        same = {"target": self.source.pk}
        for source_path, target_path in (("/docs", "/docs/deep/x"), ("/docs/deep", "/docs"), ("/", "/x"), ("/docs", "/docs")):
            with self.subTest(source_path=source_path, target_path=target_path):
                response = self.create(source_path=source_path, target_path=target_path, **same)
                self.assertEqual(response.json(), {"target_path": ["在同一个存储源里，两个文件夹不能是同一个，也不能一个在另一个里面。"]})
        with mock.patch.object(runner, "start_thread"):
            self.assertEqual(self.create(source_path="/docs", target_path="/docs-copy", **same).status_code, 201)


class TaskTests(ApiTestCase):
    def test_list_and_detail(self):
        with mock.patch.object(runner, "start_thread"):
            first = self.create().json()["id"]
        second = self.create(source_path="/docs").json()["id"]
        response = self.client.get(API)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual([task["id"] for task in data["results"]], [second, first])
        self.assertNotIn("current", data["results"][0])
        detail = self.client.get(f"{API}{first}/").json()
        self.assertEqual((detail["status"], detail["current"], detail["failures_count"]), (Status.RUNNING, [], 0))
        self.assertEqual(self.client.get(f"{API}12345/").status_code, 404)

    def test_failures(self):
        self.files(self.dst, {f"f{n:02}.txt/": None for n in range(60)})
        self.files(self.src, {f"f{n:02}.txt": b"x" for n in range(60)})
        task_id = self.create().json()["id"]
        self.assertEqual(CopyFailure.objects.filter(task_id=task_id).count(), 60)
        page = self.client.get(f"{API}{task_id}/failures/").json()
        self.assertEqual((page["count"], len(page["results"])), (60, 50))
        self.assertEqual(page["results"][0], {"id": page["results"][0]["id"], "path": "/f00.txt", "is_dir": False,
                                              "size": 1, "message": "目标文件夹里有同名的文件夹。", "attempts": 0})
        self.assertEqual(len(self.client.get(f"{API}{task_id}/failures/?page=2").json()["results"]), 10)
        detail = self.client.get(f"{API}{task_id}/").json()
        self.assertEqual((detail["status"], detail["failures_count"], detail["can_resume"]), (Status.FAILED, 60, True))

    def test_stop_and_resume(self):
        with mock.patch.object(runner, "start_thread"):
            task_id = self.create().json()["id"]
        response = self.client.post(f"{API}{task_id}/stop/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["status"], response.json()["can_stop"]), (Status.STOPPING, False))
        self.assertEqual(self.client.post(f"{API}{task_id}/stop/").status_code, 200)  # again: the same
        self.assertEqual(ActivityLog.objects.filter(kind=ActivityLog.Kind.STOP_COPY_TASK).count(), 1)
        response = self.client.post(f"{API}{task_id}/resume/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "只有失败或停止的任务才能继续。"})
        CopyTask.objects.filter(pk=task_id).update(status=Status.STOPPED)
        response = self.client.post(f"{API}{task_id}/resume/")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual((response.json()["status"], response.json()["run"]), (Status.DONE, 2))
        response = self.client.post(f"{API}{task_id}/stop/")
        self.assertEqual((response.status_code, response.json()), (409, {"detail": "这个任务没有在运行。"}))
        entry = ActivityLog.objects.get(kind=ActivityLog.Kind.RESUME_COPY_TASK)
        self.assertEqual(describe(entry), f"任务 #{task_id}；→ 存储 dst：/；第 2 次运行")
        self.assertEqual(self.client.post(f"{API}999/stop/").status_code, 404)


class PickerTests(ApiTestCase):
    def test_storages(self):
        readonly = self.storage("ro", "test-copy-readonly", self.folder("ro"))
        self.storage("off", "local", self.folder("off"), enabled=False)
        data = self.client.get(f"{API}storages/").json()
        self.assertEqual([(item["key"], item["writable"]) for item in data], [("src", True), ("dst", True), ("ro", False)])
        self.assertEqual(data[2]["id"], readonly.pk)

    def test_folders(self):
        self.files(self.src, {"docs/Zeta/": None, "docs/alpha/": None})
        data = self.client.get(f"{API}folders/", {"storage": self.source.pk, "path": "/docs"}).json()
        self.assertEqual(data, {"storage": self.source.pk, "path": "/docs", "folders": ["alpha", "deep", "Zeta"]})
        self.assertEqual(self.client.get(f"{API}folders/", {"storage": self.source.pk}).json()["folders"], ["docs", "empty"])
        self.assertEqual(self.client.get(f"{API}folders/", {"storage": self.source.pk, "path": "/nope"}).status_code, 404)
        self.assertEqual(self.client.get(f"{API}folders/", {"storage": 999}).status_code, 404)
        response = self.client.get(f"{API}folders/")
        self.assertEqual((response.status_code, response.json()), (400, {"storage": ["请选择一个存储源。"]}))
