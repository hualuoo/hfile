"""A driver for the site's tests (registered as type "fake" with FakeTypeMixin): an in-memory tree, with
knobs for what the site must cope with: a failing check, tokens that rotate (115), refresh tokens that work
once (renew), a host key learnt on the first connection (SFTP), helper buttons with choices, a QR-code poll and OAuth."""

from __future__ import annotations

import threading
import time
from unittest import mock

from apps.storages import drivers
from apps.storages.drivers.base import (
    AuthError,
    DriverError,
    Entry,
    NotADirectory,
    NotFound,
    Param,
    ReadStream,
    StorageDriver,
)


class FakeDriver(StorageDriver):
    type = "fake"
    label = "测试存储"
    params = (
        Param("endpoint", "地址", help="例如 https://fake.example.com"),
        Param("token", "令牌", kind="password", secret=True, required=False),
        Param("refresh_token", "刷新令牌", kind="password", secret=True, required=False),
        Param("expires_at", "到期时间", kind="number", secret=True, required=False, advanced=True, minimum=0),
        Param("host_key", "主机指纹", required=False),
        Param("port", "端口", kind="number", default=21, minimum=1, maximum=65535),
        Param("passive", "被动模式", kind="bool", required=False, default=True),
        Param("mode", "模式", kind="select", default="a", options=(("a", "甲"), ("b", "乙"))),
        Param("notes", "备注", kind="textarea", required=False, advanced=True),
    )
    oauth = True
    helpers = {"list": "列出", "qr": "扫码"}
    can_direct_download = True
    can_direct_upload = True

    # Knobs, set by the tests (and reset by FakeTypeMixin).
    fail_check = ""  # the DriverError message check() raises
    rotate_on_check = False  # check() refreshes: a new refresh token, like 115
    learn_host_key = False  # check() records a host key when none is set, like SFTP
    instances: list[FakeDriver] = []
    tree: dict[str, bytes | None] = {}  # storage path -> bytes (file) or None (folder)
    lock = threading.Lock()
    rotations = 0
    spent: set[str] = set()  # refresh tokens renew() used: each works once, like Baidu's and 115's

    def __init__(self, config, *, on_update=None):
        super().__init__(config, on_update=on_update)
        self.closed = False
        FakeDriver.instances.append(self)

    @classmethod
    def validate_config(cls, config):
        if str(config.get("endpoint", "")).startswith("bad"):
            return {"endpoint": "地址不对。"}
        return {}

    def check(self):
        if self.fail_check:
            raise DriverError(self.fail_check)
        if self.rotate_on_check:
            self.refresh()
        if self.learn_host_key and not self.config.get("host_key"):
            self.update_config({"host_key": "ssh-ed25519 SHA256:fake"})

    def refresh(self):
        with FakeDriver.lock:
            FakeDriver.rotations += 1
            number = FakeDriver.rotations
        if not self.config.get("refresh_token"):
            raise AuthError("还没有授权。")
        self.update_config({"token": f"access-{number}", "refresh_token": f"refresh-{number}", "expires_at": 1000 + number})

    def renew(self, stale):
        """As the drivers that keep their tokens in config renew them (Baidu's _token): under token_lock, and only
        while the access token is still `stale` (another thread or instance may have renewed it meanwhile), with a
        refresh token that works once."""
        with self.token_lock:
            if self.config.get("token") != stale:
                return
            with FakeDriver.lock:
                if self.config.get("refresh_token") in FakeDriver.spent:
                    raise AuthError("这个刷新令牌已经用过了。")
                FakeDriver.spent.add(self.config.get("refresh_token"))
            time.sleep(0.05)  # waiting for the provider: an instance coming now would send the same refresh token
            self.refresh()

    # -- files

    def _get(self, path):
        if path == "/":
            return None
        if path not in self.tree:
            raise NotFound()
        return self.tree[path]

    def list(self, path):
        if self._get(path) is not None:
            raise NotADirectory()
        prefix = "/" if path == "/" else path + "/"
        entries = []
        for item, data in self.tree.items():
            if item.startswith(prefix) and "/" not in item[len(prefix):]:
                entries.append(Entry(item.rsplit("/", 1)[1], item, data is None, None if data is None else len(data)))
        return entries

    def stat(self, path):
        data = self._get(path)
        return Entry(path.rsplit("/", 1)[1] or "/", path, data is None, None if data is None else len(data))

    def open(self, path, offset=0, length=None):
        data = self._get(path)
        if data is None:
            raise NotFound()
        part = data[offset: None if length is None else offset + length]
        return ReadStream([part], size=len(part), total=len(data), offset=offset)

    def close(self):
        self.closed = True

    # -- the form

    @classmethod
    def helper(cls, name, config, args):
        if name == "list":
            if not config.get("endpoint"):
                raise DriverError("先填地址。")
            return {"param": "mode", "choices": [{"value": "a", "label": "甲"}, {"value": "b", "label": "乙"}],
                    "message": f"来自 {args.get('origin')}"}
        if name == "qr":
            if not args.get("uid"):
                return {"image": "data:image/png;base64,AAAA", "poll": "qr",
                        "args": {"uid": "u1", "verifier": "pkce-secret"}, "message": "请扫码"}
            if args.get("verifier") != "pkce-secret":
                raise DriverError("参数不对。")
            return {"changes": {"token": "qr-access", "refresh_token": "qr-refresh", "expires_at": 4242,
                                "host_key": "learnt"}, "message": "授权成功"}
        raise DriverError("没有这个按钮。")

    @classmethod
    def authorize_url(cls, config, *, redirect_uri, state):
        if not config.get("endpoint"):
            raise DriverError("先填地址。")
        return f"https://login.fake.example/authorize?state={state}&redirect_uri={redirect_uri}"

    @classmethod
    def exchange_code(cls, config, *, code, redirect_uri):
        if code != "good-code":
            raise AuthError("授权码不对。")
        return {"refresh_token": f"oauth-refresh-for-{config.get('endpoint')}", "unknown": "dropped"}


class FakeTypeMixin:
    """Registers the "fake" type for the test and resets its knobs; closes every instance afterwards."""

    def setUp(self):
        super().setUp()
        from apps.storages import instances

        patcher = mock.patch.dict(drivers.DRIVERS, {"fake": "apps.storages.tests.fakes:FakeDriver"})
        patcher.start()
        self.addCleanup(patcher.stop)
        grace = mock.patch.object(instances, "GRACE", 0)
        grace.start()
        self.addCleanup(grace.stop)
        FakeDriver.fail_check = ""
        FakeDriver.rotate_on_check = False
        FakeDriver.learn_host_key = False
        FakeDriver.instances = []
        FakeDriver.tree = {}
        FakeDriver.rotations = 0
        FakeDriver.spent = set()
        instances.close_all()
        self.addCleanup(instances.close_all)
