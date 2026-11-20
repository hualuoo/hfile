"""Direct uploads: the browser sends the bytes to the backend as the driver's UploadTarget says, then
POST complete makes the site look at the file. Against moto (S3: one PUT), the drv-cloud fake of
Microsoft Graph (OneDrive: chunks with Content-Range) and the fake 又拍云 (a form POST). The tests play
the browser's part with httpx."""

import json
import logging
import os
import uuid
from unittest import mock

import httpx

from apps.activity.testing import entries
from apps.storages import config as storage_config
from apps.storages.drivers import microsoft
from apps.storages.drivers.s3 import S3Driver
from apps.storages.drivers.upyun import UpYunDriver
from apps.storages.models import StorageSource
from apps.storages.tests.fake_graph import FakeGraph
from apps.storages.tests.fake_upyun import FakeUpYun
from apps.storages.tests.moto_s3 import MotoS3
from apps.uploads import spool
from apps.uploads.csp import connect_sources
from apps.uploads.models import Kind, UploadSession

from .base import API, EVERYTHING, UploadTestCase

moto: MotoS3
graph: FakeGraph
upyun: FakeUpYun


def setUpModule():
    global moto, graph, upyun
    logging.getLogger("hfile.storage").addHandler(logging.NullHandler())
    moto = MotoS3()
    graph = FakeGraph()
    upyun = FakeUpYun()


def tearDownModule():
    moto.stop()
    graph.close()
    upyun.stop()


class DirectTestCase(UploadTestCase):
    def remote(self, key, type_, cls, values, **fields):
        storage = StorageSource.objects.create(key=key, name=key, type=type_,
                                               config=storage_config.encode(cls, values), **fields)
        self.grant(self.alice, EVERYTHING, storage=storage)
        return storage

    def create_in(self, storage, name, size, folder="/", **more):
        body = {"storage": storage.key, "folder": folder, "name": name, "size": size, **more}
        return self.client.post(API, json.dumps(body), content_type="application/json")

    def complete(self, session):
        return self.client.post(session["complete_url"], {}, content_type="application/json")


class S3Tests(DirectTestCase):
    def setUp(self):
        super().setUp()
        self.bucket = moto.bucket()
        self.s3 = self.remote("bucket", "s3", S3Driver, {
            "endpoint": moto.endpoint, "access_key": "AKIDTEST", "secret_key": "secret-test-key", "bucket": self.bucket,
            "base_path": "/", "path_style": "path", "is_private": True, "domain": "", "region": ""})

    def test_one_put_then_complete(self):
        data = os.urandom(5000)
        made = self.create_in(self.s3, "照片.jpg", len(data), "/album")
        self.assertEqual(made.status_code, 201, made.content)
        session = made.json()
        self.assertEqual((session["kind"], session["state"]), ("direct", "waiting"))
        target = session["target"]
        self.assertEqual((target["kind"], target["method"]), ("single", "PUT"))
        self.assertTrue(target["url"].startswith(f"{moto.endpoint}/{self.bucket}/"))
        self.assertEqual(target["headers"], {"Content-Type": "image/jpeg"})
        self.assertFalse(spool.path_of(session["id"]).exists())  # nothing waits on the site
        # The target is answered once; it isn't kept.
        self.assertNotIn("target", self.client.get(session["url"]).json())
        sent = httpx.put(target["url"], content=data, headers=target["headers"], timeout=30)
        self.assertEqual(sent.status_code, 200, sent.text)
        done = self.complete(session)
        self.assertEqual(done.status_code, 200, done.content)
        body = done.json()
        self.assertEqual((body["state"], body["item"]["size"], body["item"]["path"]), ("done", 5000, "/album/照片.jpg"))
        self.assertIn("album/照片.jpg", moto.keys(self.bucket))
        self.assertEqual(self.signals, [("bucket", ["/album", "/album/照片.jpg"])])
        self.assertEqual(self.complete(session).json()["state"], "done")  # asked again: logged once
        self.assertEqual([(entry.storage, entry.path, entry.detail) for entry in entries("upload")],
                         [(self.s3, "/album/照片.jpg", {"size": 5000})])

    def test_complete_before_the_bytes_and_a_wrong_size(self):
        session = self.create_in(self.s3, "a.bin", 100).json()
        self.assertCode(self.complete(session), 409, "missing")
        self.assertEqual(self.client.get(session["url"]).json()["state"], "failed")
        httpx.put(session["target"]["url"], content=b"x" * 60, headers=session["target"]["headers"], timeout=30)
        self.assertCode(self.complete(session), 409, "size_mismatch")
        httpx.put(session["target"]["url"], content=b"x" * 100, headers=session["target"]["headers"], timeout=30)
        self.assertEqual(self.complete(session).json()["state"], "done")

    def test_overwrite_and_d17(self):
        moto.put_raw(self.bucket, "a.bin", b"old")
        self.assertCode(self.create_in(self.s3, "a.bin", 10), 409, "exists")
        self.grant(self.alice, [code for code in EVERYTHING if code != "delete"], storage=self.s3)
        self.assertCode(self.create_in(self.s3, "a.bin", 10, overwrite=True), 403, "permission_denied")
        self.grant(self.alice, EVERYTHING, storage=self.s3)
        self.assertEqual(self.create_in(self.s3, "a.bin", 10, overwrite=True).json()["kind"], "direct")

    def test_a_parent_that_is_a_file(self):
        """An object store's write() doesn't check that the folder above is a file: the site does."""
        moto.put_raw(self.bucket, "notes", b"a file")
        self.assertCode(self.create_in(self.s3, "a.txt", 10, "/notes"), 409, "parent_is_file")

    def test_proxy_upload_on(self):
        self.s3.proxy_upload = True
        self.s3.save()
        data = os.urandom(2500)
        made = self.create_in(self.s3, "p.bin", len(data))
        session = made.json()
        self.assertEqual(session["kind"], "proxy")
        for start in range(0, len(data), session["chunk_size"]):
            answer = self.put(session, data[start:start + session["chunk_size"]], start, len(data))
        self.assertEqual(answer.status_code, 201, answer.content)
        self.assertIn("p.bin", moto.keys(self.bucket))

    def test_too_large_for_a_direct_upload(self):
        with mock.patch.object(S3Driver, "direct_upload_limit", 100):
            self.assertEqual(self.create_in(self.s3, "big.bin", 101).json()["kind"], "proxy")
            self.assertEqual(self.create_in(self.s3, "small.bin", 100).json()["kind"], "direct")

    def test_an_empty_file_goes_through_the_site(self):
        body = self.create_in(self.s3, "empty.txt", 0).json()
        self.assertEqual((body["kind"], body["state"]), ("proxy", "done"))
        self.assertIn("empty.txt", moto.keys(self.bucket))

    def test_the_page_may_connect_to_it(self):
        self.assertEqual(connect_sources(self.s3), ["https:", moto.endpoint])
        self.assertEqual(connect_sources(self.storage), [])  # local: through the site
        self.s3.proxy_upload = True
        self.assertEqual(connect_sources(self.s3), [])

    def test_a_grant_revoked_before_complete(self):
        session = self.create_in(self.s3, "a.bin", 10).json()
        httpx.put(session["target"]["url"], content=b"x" * 10, headers=session["target"]["headers"], timeout=30)
        self.grant(self.alice, [code for code in EVERYTHING if code != "upload"], storage=self.s3)
        self.assertCode(self.complete(session), 403, "permission_denied")


class OneDriveTests(DirectTestCase):
    def setUp(self):
        super().setUp()
        for name, value in {"graph_root": graph.url, "login_root": graph.url, "retry_delay": 0.01,
                            "upload_chunk": 320 * 1024}.items():
            patcher = mock.patch.object(microsoft.OneDriveDriver, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        base = f"/t{uuid.uuid4().hex[:8]}"
        drive = graph.drive("me")
        with graph.lock:
            drive.add(drive.root, base[1:], True)
        self.drive = self.remote("od", "onedrive", microsoft.OneDriveDriver, {
            "client_id": "app-id", "client_secret": "app-secret", "tenant": "common",
            "refresh_token": graph.issue_refresh_token(), "base_path": base, "download_domain": ""})
        self.base = base

    def test_chunks_then_complete(self):
        data = os.urandom(700 * 1024)
        made = self.create_in(self.drive, "大文件.bin", len(data), "/", relative_path="sub")
        self.assertEqual(made.status_code, 201, made.content)
        session = made.json()
        target = session["target"]
        self.assertEqual((target["kind"], target["method"], target["chunk_size"]), ("chunks", "PUT", 320 * 1024))
        statuses = []
        for start in range(0, len(data), target["chunk_size"]):
            piece = data[start:start + target["chunk_size"]]
            sent = httpx.put(target["url"], content=piece, timeout=30,
                             headers={"Content-Range": f"bytes {start}-{start + len(piece) - 1}/{len(data)}"})
            statuses.append(sent.status_code)
        self.assertEqual(statuses[:-1], [202] * (len(statuses) - 1))
        self.assertIn(statuses[-1], (200, 201))
        done = self.complete(session)
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(done.json()["item"]["size"], len(data))
        with graph.lock:
            item = graph.drive("me").find(f"{self.base}/sub/大文件.bin")
        self.assertEqual(item.data, data)

    def test_an_unfinished_upload_session(self):
        data = os.urandom(700 * 1024)
        session = self.create_in(self.drive, "half.bin", len(data)).json()
        target = session["target"]
        httpx.put(target["url"], content=data[:320 * 1024], timeout=30,
                  headers={"Content-Range": f"bytes 0-{320 * 1024 - 1}/{len(data)}"})
        self.assertCode(self.complete(session), 409, "missing")

    def test_names_onedrive_refuses(self):
        self.assertCode(self.create_in(self.drive, "a:b.txt", 10), 400, "invalid_name")
        self.assertCode(self.create_in(self.drive, "con", 10), 400, "invalid_path")  # the driver's own rules

    def test_an_empty_file(self):
        body = self.create_in(self.drive, "empty.txt", 0).json()
        self.assertEqual((body["kind"], body["state"]), ("proxy", "done"))
        with graph.lock:
            self.assertEqual(graph.drive("me").find(f"{self.base}/empty.txt").data, b"")


class UpYunTests(DirectTestCase):
    def setUp(self):
        super().setUp()
        upyun.reset()
        patcher = mock.patch.object(UpYunDriver, "api_url", upyun.api.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.up = self.remote("up", "upyun", UpYunDriver, {
            "bucket": upyun.bucket, "operator": upyun.operator, "password": upyun.password, "base_path": "/",
            "domain": upyun.cdn.url, "token": ""})

    def test_a_form_post_then_complete(self):
        data = os.urandom(3000)
        session = self.create_in(self.up, "表单.bin", len(data), "/f").json()
        target = session["target"]
        self.assertEqual((target["kind"], target["method"], target["file_field"]), ("form", "POST", "file"))
        self.assertEqual(set(target["fields"]), {"policy", "authorization"})
        sent = httpx.post(target["url"], data=target["fields"], files={target["file_field"]: ("表单.bin", data)}, timeout=30)
        self.assertEqual(sent.status_code, 200, sent.text)
        done = self.complete(session)
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(upyun.files["/f/表单.bin"], data)

    def test_braces_go_through_the_site(self):
        """又拍云's form API would read {year} in the name: the driver gives no target, the site takes it."""
        session = self.create_in(self.up, "{year}.txt", 4).json()
        self.assertEqual(session["kind"], "proxy")
        self.assertEqual(self.put(session, b"1234", 0).status_code, 201)
        self.assertEqual(upyun.files["/{year}.txt"], b"1234")

    def test_kinds(self):
        self.assertEqual(UploadSession.objects.filter(kind=Kind.DIRECT).count(), 0)
