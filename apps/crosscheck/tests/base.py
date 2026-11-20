"""What the cross-checks share: a local storage "disk" on a folder of its own, the actors (the guest,
alice and bob as normal users, a staff admin, a superuser), and helpers that speak every endpoint that
takes a storage and a path, as the browser and the share page do.

The storage's folder (self.root) holds:

    docs/a.txt  docs/b.txt  docs/sub/c.txt  top.txt
    home/alice/mine.txt  home/alice/docs/note.txt  home/bob/bob.txt

alice may do everything (but the two ignore_ permissions) at "/", bob the same at /home/bob, the staff
admin the same at "/"; the guest has nothing until a test gives him a grant. Folders outside the
storage (self.outside) are for the symlink tests. Files live beside the config.toml the tests run with
(crosscheck-tests/), like the uploads tests' own.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.permissions import ALL
from apps.links import ratelimit
from apps.shares.models import FILE, FOLDER, Share
from apps.siteconf.models import SiteSettings
from apps.storages import config as storage_config
from apps.storages import instances
from apps.storages.drivers.local import LocalDriver
from apps.storages.models import FilterMode, FilterRule, FolderPassword, Grant, StorageSource
from apps.uploads import service as uploads_service

EVERYTHING = [code for code in ALL if not code.startswith("ignore_")]
FILES_API = "/api/v1/files/"
SHARES_API = "/api/v1/shares/"

TREE = {
    "docs/a.txt": b"alpha",
    "docs/b.txt": b"bravo",
    "docs/sub/c.txt": b"charlie",
    "top.txt": b"top",
    "home/alice/mine.txt": b"alice's",
    "home/alice/docs/note.txt": b"note",
    "home/bob/bob.txt": b"bob's",
}


def but(*codes):
    """EVERYTHING without these permission codes."""
    return [code for code in EVERYTHING if code not in codes]


def test_folder() -> Path:
    base = Path(settings.CONFIG_FILE).resolve().parent / "crosscheck-tests"
    try:
        base.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="t-", dir=base))
    except OSError:
        return Path(tempfile.mkdtemp(prefix="hfile-crosscheck-"))


def path_of(url: str) -> str:
    """The path and query of a URL the API answered (absolute ones too), as the browser would ask it."""
    parts = urllib.parse.urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def query_of(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)


class CrossCase(TestCase):
    chunk = 1000  # bytes per upload chunk (apps.uploads.service.CHUNK_SIZE)

    def setUp(self):
        super().setUp()
        self.tmp = test_folder()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_bytes(b"outside secret")
        (self.outside / "folder").mkdir()
        (self.outside / "folder" / "inner.txt").write_bytes(b"outside inner")
        spool = override_settings(UPLOAD_SPOOL_DIR=str(self.tmp / "spool"))
        spool.enable()
        self.addCleanup(spool.disable)
        for patcher in (mock.patch.object(uploads_service, "CHUNK_SIZE", self.chunk),
                        mock.patch.object(uploads_service, "_light_cleanup", lambda: None),
                        mock.patch.object(instances, "GRACE", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Driver instances are kept per storage id, and SQLite gives a rolled-back id again.
        instances.close_all()
        self.addCleanup(instances.close_all)
        for limiter in (ratelimit.limiter, ratelimit.misses, ratelimit.opened):
            limiter.reset()
            self.addCleanup(limiter.reset)
        self.files(TREE)
        self.storage = self.make_storage("disk", self.root)
        self.guest = User.objects.guest()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.bob = User.objects.create_user("bob", password="pw-bob-2026")
        self.staff = User.objects.create_user("staff", password="pw-staff-2026", is_staff=True)
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.grant(self.alice, EVERYTHING)
        self.grant(self.bob, EVERYTHING, root="/home/bob")
        self.grant(self.staff, EVERYTHING)
        self.anon = APIClient()
        self.as_alice = self.client_for(self.alice)
        self.as_bob = self.client_for(self.bob)
        self.as_staff = self.client_for(self.staff)

    # -- making things

    def make_storage(self, key, root, *, external=False, **fields) -> StorageSource:
        return StorageSource.objects.create(
            key=key, name=f"存储 {key}", type="local",
            config=storage_config.encode(LocalDriver, {"root": str(root), "allow_external_symlinks": external}),
            **fields)

    def files(self, tree, root=None):
        base = Path(root or self.root)
        for name, data in tree.items():
            path = base / name
            if data is None:
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def client_for(self, user=None) -> APIClient:
        client = APIClient()
        if user is not None:
            client.force_login(user)
        return client

    def grant(self, user, permissions=EVERYTHING, root="/", enabled=True, storage=None) -> Grant:
        grant, _ = Grant.objects.update_or_create(
            user=user, storage=storage or self.storage,
            defaults={"permissions": list(permissions), "root_path": root, "enabled": enabled})
        return grant

    def hide(self, expression, mode=FilterMode.HIDDEN, position=0, storage=None) -> FilterRule:
        return FilterRule.objects.create(storage=storage or self.storage, expression=expression, mode=mode,
                                         position=position)

    def lock(self, expression, password="pw1", position=0, storage=None) -> FolderPassword:
        return FolderPassword.objects.create(storage=storage or self.storage, expression=expression,
                                             password=password, position=position)

    def change_password(self, rule: FolderPassword, password: str) -> None:
        """As the admin's rules page does it: a new revision (apps/storages/views.py RulesView)."""
        rule.password = password
        rule.revision += 1
        rule.save()

    def site(self, **fields) -> SiteSettings:
        site = SiteSettings.load()
        for name, value in fields.items():
            setattr(site, name, value)
        site.save()
        return site

    def disable_storage(self, storage=None):
        storage = storage or self.storage
        storage.enabled = False
        storage.save()

    # -- answers

    @staticmethod
    def code(response):
        try:
            data = response.json()
        except (ValueError, TypeError):
            return None
        return data.get("code") if isinstance(data, dict) else None

    @staticmethod
    def body(response) -> bytes:
        return b"".join(response.streaming_content) if response.streaming else response.content

    def assertRefused(self, response, status=None, code=None, msg=None):
        """Not a success: 4xx (or this status), with this code when given."""
        detail = f"{msg or ''} {response.status_code} {self.body(response)[:300]!r}"
        if status is None:
            self.assertGreaterEqual(response.status_code, 400, detail)
        else:
            self.assertEqual(response.status_code, status, detail)
        if code is not None:
            self.assertEqual(self.code(response), code, detail)

    def assertBatchRefused(self, response, msg=None):
        """A move, copy or delete that did nothing: refused as a whole, or every item failed."""
        if response.status_code >= 400:
            return
        self.assertEqual(response.status_code, 200, msg)
        self.assertEqual(response.json()["succeeded"], 0, f"{msg or ''} {response.json()}")

    def assertOk(self, response, status=200, msg=None):
        self.assertEqual(response.status_code, status, f"{msg or ''} {self.body(response)[:500]!r}")
        return response

    # -- the files API (paths relative to the caller's root)

    def list_(self, client, path="/", key="disk", **headers):
        return client.get(f"{FILES_API}{key}/list/", {"path": path}, **headers)

    def names(self, client, path="/", key="disk"):
        response = self.assertOk(self.list_(client, path, key))
        return sorted(item["name"] for item in response.json()["items"])

    def items(self, client, path="/", key="disk"):
        response = self.assertOk(self.list_(client, path, key))
        return {item["name"]: item for item in response.json()["items"]}

    def item(self, client, path, key="disk", **headers):
        return client.get(f"{FILES_API}{key}/item/", {"path": path}, **headers)

    def _post(self, client, url, body):
        return client.post(url, json.dumps(body), content_type="application/json")

    def mkdir(self, client, path, name, key="disk", **body):
        return self._post(client, f"{FILES_API}{key}/mkdir/", {"path": path, "name": name, **body})

    def rename(self, client, path, name, new_name, key="disk", **body):
        return self._post(client, f"{FILES_API}{key}/rename/", {"path": path, "name": name, "new_name": new_name, **body})

    def move(self, client, path, names, target, key="disk", **body):
        return self._post(client, f"{FILES_API}{key}/move/", {"path": path, "names": names, "target": target, **body})

    def copy(self, client, path, names, target, key="disk", **body):
        return self._post(client, f"{FILES_API}{key}/copy/", {"path": path, "names": names, "target": target, **body})

    def delete(self, client, path, names, key="disk", **body):
        return self._post(client, f"{FILES_API}{key}/delete/", {"path": path, "names": names, **body})

    def batch_ok(self, response) -> bool:
        """A move, copy or delete that answered 200 with every item done."""
        return response.status_code == 200 and response.json()["failed"] == 0

    def batch_codes(self, response) -> list:
        self.assertOk(response)
        return [result["code"] for result in response.json()["results"] if not result["ok"]]

    def unlock(self, client, path=None, password="pw1", remember=False, rule=None, key="disk"):
        body = {"password": password, "remember": remember}
        if rule is not None:
            body["rule"] = rule
        else:
            body["path"] = path
        return self._post(client, f"/api/v1/storages/{key}/unlock/", body)

    def forget(self, client):
        return client.delete("/api/v1/storages/unlocks/")

    def file_url(self, client, path, key="disk"):
        """A file's `url` in its folder's listing (None when it has none)."""
        folder, _, name = path.rpartition("/")
        return self.items(client, folder or "/", key)[name]["url"]

    def inline_url(self, client, path, key="disk"):
        response = self.assertOk(self.item(client, path, key))
        return response.json()["inline_url"]

    def fetch(self, client, url, **headers):
        """GET of a URL the site handed out: (response, body)."""
        response = client.get(path_of(url), **headers)
        return response, self.body(response)

    # -- uploads

    def upload_session(self, client, folder, name, size, key="disk", **more):
        return self._post(client, "/api/v1/uploads/", {"storage": key, "folder": folder, "name": name, "size": size,
                                                       **more})

    def put_chunk(self, client, session, data: bytes, start: int, total: int):
        end = start + len(data) - 1
        return client.put(session["url"], data=data, content_type="application/octet-stream",
                          HTTP_CONTENT_RANGE=f"bytes {start}-{end}/{total}")

    def upload(self, client, folder, name, data: bytes, key="disk", **more):
        """The whole proxied upload, as the panel sends it: the session's answer when it is refused,
        else the last chunk's."""
        made = self.upload_session(client, folder, name, len(data), key, **more)
        if made.status_code != 201:
            return made
        session = made.json()
        answer = made
        for start in range(0, len(data), session["chunk_size"]):
            answer = self.put_chunk(client, session, data[start:start + session["chunk_size"]], start, len(data))
            if answer.status_code not in (201, 202):
                return answer
        return answer

    def save_text(self, client, path, content, key="disk", **more):
        return client.put("/api/v1/uploads/text/", json.dumps({"storage": key, "path": path, "content": content,
                                                               **more}), content_type="application/json")

    def make_folders(self, client, folder, folders, key="disk", **more):
        return self._post(client, "/api/v1/uploads/folders/", {"storage": key, "folder": folder, "folders": folders,
                                                              **more})

    # -- links

    def path_links(self, client, paths, key="disk", **more):
        return self._post(client, f"/api/v1/links/{key}/path/", {"paths": paths, **more})

    def short_links(self, client, paths, expires=86400, key="disk", **more):
        return self._post(client, f"/api/v1/links/{key}/short/", {"paths": paths, "expires": expires, **more})

    def path_link(self, client, path, key="disk"):
        response = self.assertOk(self.path_links(client, [path], key))
        return path_of(response.json()["links"][0]["url"])

    def short_link(self, client, path, key="disk", expires=86400):
        response = self.assertOk(self.short_links(client, [path], expires, key), 201)
        return path_of(response.json()["links"][0]["url"])

    def follow(self, client, url):
        """Opens a link (/s/..., /directlink/...): (its answer, the body at the Location it sends to, if any)."""
        response = client.get(url)
        if response.status_code in (301, 302) and response["Location"].startswith("/"):
            target, body = self.fetch(client, response["Location"])
            return response, target, body
        return response, None, self.body(response)

    # -- shares

    def share_create(self, client, path, entries, **more):
        """POST /api/v1/shares/: entries are [(name, type)]."""
        return self._post(client, SHARES_API, {"storage": more.pop("storage", "disk"), "path": path,
                                              "entries": [{"name": name, "type": kind} for name, kind in entries],
                                              **more})

    def share_of(self, owner, folder, entries, password="", storage=None, key=None, expires_at=None) -> Share:
        """A share stored directly (nothing checked), as `owner` made it."""
        share = Share(key=key or f"s{Share.objects.count() + 1:07d}", storage=storage or self.storage, folder=folder,
                      entries=[{"name": name, "type": kind} for name, kind in entries],
                      names="\n".join(name for name, _ in entries), owner=owner, expires_at=expires_at)
        share.set_password(password)
        share.save()
        return share

    def vinfo(self, client, share):
        return client.get(f"{SHARES_API}{share.key}/")

    def vverify(self, client, share, password):
        return self._post(client, f"{SHARES_API}{share.key}/verify/", {"password": password})

    def vlist(self, client, share, path="/"):
        return client.get(f"{SHARES_API}{share.key}/list/", {"path": path})

    def vnames(self, client, share, path="/"):
        response = self.assertOk(self.vlist(client, share, path))
        return sorted(item["name"] for item in response.json()["items"])

    def vitems(self, client, share, path="/"):
        response = self.assertOk(self.vlist(client, share, path))
        return {item["name"]: item for item in response.json()["items"]}

    def vitem(self, client, share, path):
        return client.get(f"{SHARES_API}{share.key}/item/", {"path": path})

    def vdownload(self, client, share, path, **headers):
        return client.get(f"/share/{share.key}/download", {"path": path}, **headers)

    def vpreview(self, client, share, path, **headers):
        return client.get(f"/share/{share.key}/preview", {"path": path}, **headers)

    def vunlock(self, client, share, path=None, password="pw1", rule=None):
        body = {"password": password}
        if rule is not None:
            body["rule"] = rule
        else:
            body["path"] = path
        return self._post(client, f"{SHARES_API}{share.key}/unlock/", body)

    def served(self, client, response):
        """Where a share's /download or /preview redirect leads: (answer, body); (response, b"") when it
        didn't redirect to this site."""
        if response.status_code != 302 or not response["Location"].startswith("/"):
            return response, b""
        return self.fetch(client, response["Location"])

    # -- the preview sources

    def preview_markdown(self, client, url):
        return self._post(client, "/api/v1/preview/markdown/", {"url": url})

    def kkfileview(self, client, url):
        return self._post(client, "/api/v1/preview/kkfileview/", {"url": url})


__all__ = ["EVERYTHING", "FILE", "FOLDER", "CrossCase", "FilterMode", "but", "path_of", "query_of"]
