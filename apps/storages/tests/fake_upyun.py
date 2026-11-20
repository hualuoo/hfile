"""An in-memory UpYun for driver tests: the REST API (v0.api.upyun.com) and a bound domain.

The API checks what UpYun checks: the Date header (30 minutes), the UPYUN signature over
METHOD&URI&Date[&Content-MD5] with the URI as sent, the service name, Content-Length on uploads
(no chunked bodies), folders that only go when empty, files-only copies and moves, the parallel
resumable upload and the FORM API's policy and signature. The domain serves files and checks the
`_upt` token anti-leech signature when a key is set.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import threading
import time
import uuid
from email import policy as email_policy
from email.parser import BytesParser
from urllib.parse import parse_qs, quote, unquote, urlsplit

from apps.storages.tests.fake_http import Handler, Server

END_MARK = "g2gCZAAEbmV4dGQAA2VvZg"


class FakeUpYun:
    def __init__(self, bucket: str = "hfile-test", operator: str = "tester", password: str = "pass word"):
        self.bucket, self.operator, self.password = bucket, operator, password
        self.lock = threading.RLock()
        self.token = ""  # the domain's anti-leech key
        self.reset()
        self.api = Server(ApiHandler, self)
        self.cdn = Server(CdnHandler, self)

    def reset(self) -> None:
        with self.lock:
            self.files: dict[str, bytes] = {}
            self.dates: dict[str, int] = {}
            self.folders: set[str] = {"/"}
            self.uploads: dict[str, dict] = {}
            self.requests: list[tuple[str, str, dict[str, str]]] = []
            # (method or None for any, status): answer the next such request with that status
            self.fail: list[tuple[str | None, int]] = []
            self.skew = 0  # seconds our clock is ahead of the driver's

    def stop(self) -> None:
        self.api.stop()
        self.cdn.stop()

    def now(self) -> int:
        return int(time.time()) + self.skew

    def sign(self, message: str) -> str:
        key = hashlib.md5(self.password.encode()).hexdigest().encode()
        return base64.b64encode(hmac.new(key, message.encode(), hashlib.sha1).digest()).decode()

    def children(self, folder: str) -> list[tuple[str, bool]]:
        prefix = folder.rstrip("/") + "/"
        names = {(path[len(prefix):], True) for path in self.folders if path != "/" and path.startswith(prefix)
                 and "/" not in path[len(prefix):]}
        names |= {(path[len(prefix):], False) for path in self.files if path.startswith(prefix)
                  and "/" not in path[len(prefix):]}
        return sorted(names)

    def make_parents(self, path: str) -> None:
        parts = [part for part in path.split("/")[:-1] if part]
        for index in range(len(parts)):
            folder = "/" + "/".join(parts[:index + 1])
            if folder in self.files:
                raise ValueError(folder)
            self.folders.add(folder)
            self.dates.setdefault(folder, self.now())

    def parent_exists(self, path: str) -> bool:
        parent = path.rsplit("/", 1)[0] or "/"
        return parent in self.folders


class ApiHandler(Handler):
    def date_time_string(self, timestamp=None):
        """The Date header of every answer, from the fake's clock."""
        return email.utils.formatdate(self.fake.now() if timestamp is None else timestamp, usegmt=True)

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def error(self, status: int, code: int, message: str, headers: dict[str, str] | None = None) -> None:
        if self.command == "HEAD":
            return self.answer(status, b"", headers)
        self.answer_json(status, {"msg": message, "code": code, "id": uuid.uuid4().hex}, headers)

    def dispatch(self):
        fake = self.fake
        raw_path = self.path.split("?", 1)[0]
        if self.headers.get("Transfer-Encoding"):
            return self.error(400, 40000011, "chunked request bodies not supported yet")
        body = self.body()
        with fake.lock:
            fake.requests.append((self.command, raw_path, dict(self.headers)))
            failure = None
            for index, (method, _status) in enumerate(fake.fail):
                if method in (None, self.command):
                    failure = fake.fail.pop(index)[1]
                    break
        if failure:
            return self.error(failure, 42900001 if failure == 429 else 50300000, "fake failure")
        if self.command == "POST" and raw_path.rstrip("/") == f"/{fake.bucket}" \
                and self.headers.get("Content-Type", "").startswith("multipart/form-data"):
            return self.form_upload(body)
        date = self.headers.get("Date")
        if not date:
            return self.error(401, 40100001, "need date header")
        try:
            sent = email.utils.parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError):
            return self.error(401, 40100016, "invalid date value in header")
        if abs(sent - fake.now()) > 1800:
            return self.error(401, 40100002, "date offset error")
        message = f"{self.command}&{raw_path}&{date}"
        if self.headers.get("Content-MD5"):
            message += "&" + self.headers["Content-MD5"]
        if self.headers.get("Authorization") != f"UPYUN {fake.operator}:{fake.sign(message)}":
            return self.error(401, 40100005, "signature error")
        bucket, _, rest = raw_path[1:].partition("/")
        if bucket != fake.bucket:
            return self.error(401, 40100012, "bucket not exist")
        try:
            path = "/" + unquote(rest, errors="strict").rstrip("/")
        except UnicodeDecodeError:
            return self.error(400, 40000030, "path encoding should be utf8")
        with fake.lock:
            handler = getattr(self, "op_" + self.command)
            return handler(path, raw_path.endswith("/"), body)

    # -- operations

    def op_HEAD(self, path, as_folder, body):
        fake = self.fake
        if path in fake.folders:
            return self.answer(200, b"", {"x-upyun-file-type": "folder", "x-upyun-file-size": "0",
                                          "x-upyun-file-date": str(fake.dates.get(path, 0))})
        if path in fake.files:
            data = fake.files[path]
            return self.answer(200, b"", {"x-upyun-file-type": "file", "x-upyun-file-size": str(len(data)),
                                          "x-upyun-file-date": str(fake.dates[path]),
                                          "Content-Md5": hashlib.md5(data).hexdigest(), "Content-Length": "0"})
        return self.error(404, 40400001, "file or directory not found")

    def op_GET(self, path, as_folder, body):
        fake = self.fake
        if as_folder or path in fake.folders:
            if path not in fake.folders:
                return self.error(404, 40400001, "file or directory not found")
            if self.headers.get("Accept") != "application/json":
                return self.error(400, 40001008, "list type not support")
            limit = int(self.headers.get("x-list-limit") or 100)
            if not 0 < limit <= 10000:
                return self.error(400, 40001005, "iter args invalid")
            start = int(base64.b64decode(self.headers["x-list-iter"]).decode()) if self.headers.get("x-list-iter") else 0
            children = fake.children(path)
            page = children[start:start + limit]
            files = [{"type": "folder" if is_dir else "text/plain", "name": name,
                      "length": 0 if is_dir else len(fake.files[f"{path.rstrip('/')}/{name}"]),
                      "last_modified": fake.dates.get(f"{path.rstrip('/')}/{name}", 0)} for name, is_dir in page]
            mark = END_MARK if start + limit >= len(children) else base64.b64encode(str(start + limit).encode()).decode()
            return self.answer_json(200, {"files": files, "iter": mark}, {"x-upyun-list-iter": mark})
        if path not in fake.files:
            return self.error(404, 40400001, "file or directory not found")
        data = fake.files[path]
        range_ = self.headers.get("Range")
        if range_:
            first, _, last = range_.removeprefix("bytes=").partition("-")
            first, last = int(first), int(last) if last else len(data) - 1
            if first >= len(data):
                return self.error(400, 40001161, "wrong range")
            return self.answer(206, data[first:last + 1], {"Content-Range": f"bytes {first}-{last}/{len(data)}",
                                                           "Content-Type": "application/octet-stream"})
        return self.answer(200, data, {"Content-Type": "application/octet-stream"})

    def op_PUT(self, path, as_folder, body):
        fake = self.fake
        headers = self.headers
        if headers.get("X-Upyun-Multi-Stage"):
            return self.multipart(path, body)
        source_header = headers.get("X-Upyun-Copy-Source") or headers.get("X-Upyun-Move-Source")
        if source_header:
            bucket, _, rest = source_header[1:].partition("/")
            source = "/" + unquote(rest)
            if bucket != fake.bucket or source not in fake.files:
                return self.error(400 if source in fake.folders else 404, 40000042 if source in fake.folders else 40400001,
                                  "source key wrong" if source in fake.folders else "file or directory not found")
            if source == path:
                return self.error(400, 40000041, "source key equals to target key")
            if not fake.parent_exists(path):
                return self.error(404, 40400001, "file or directory not found")
            fake.files[path] = fake.files[source]
            fake.dates[path] = fake.now()
            if headers.get("X-Upyun-Move-Source"):
                del fake.files[source]
            return self.answer(200)
        if headers.get("folder") == "true":
            return self.op_POST(path, as_folder, body)
        if headers.get("Content-Length") is None:
            return self.error(403, 40300026, "need content-length header")
        if path in fake.folders:
            return self.error(403, 40300015, "path is not a file, maybe a directory")
        if not fake.parent_exists(path):
            if headers.get("mkdir") != "true":
                return self.error(404, 40400001, "file or directory not found")
            try:
                fake.make_parents(path)
            except ValueError:
                return self.error(406, 40600001, "dir not acceptable")
        fake.files[path] = body
        fake.dates[path] = fake.now()
        return self.answer(200)

    def op_POST(self, path, as_folder, body):
        fake = self.fake
        if self.headers.get("folder") != "true":
            return self.error(400, 40001004, "folder args invalid")
        if path in fake.folders:
            return self.error(406, 40600002, "folder already exists")
        if path in fake.files:
            return self.error(406, 40600001, "dir not acceptable")
        if not fake.parent_exists(path):
            if self.headers.get("mkdir") != "true":
                return self.error(404, 40400001, "file or directory not found")
            try:
                fake.make_parents(path)
            except ValueError:
                return self.error(406, 40600001, "dir not acceptable")
        fake.folders.add(path)
        fake.dates[path] = fake.now()
        return self.answer(200)

    def op_DELETE(self, path, as_folder, body):
        fake = self.fake
        if path in fake.files:
            del fake.files[path]
            return self.answer(200)
        if path in fake.folders and path != "/":
            if fake.children(path):
                return self.error(403, 40300005, "directory not empty")
            fake.folders.discard(path)
            return self.answer(200)
        return self.error(404, 40400001, "file or directory not found")

    def multipart(self, path, body):
        fake = self.fake
        stage = self.headers["X-Upyun-Multi-Stage"]
        if stage == "initiate":
            part_size = int(self.headers.get("X-Upyun-Multi-Part-Size") or 1024 * 1024)
            if self.headers.get("X-Upyun-Multi-Disorder") != "true" or part_size % (1024 * 1024) \
                    or part_size > 50 * 1024 * 1024:
                return self.error(400, 40001027, "wrong multipart size")
            if not fake.parent_exists(path):
                return self.error(404, 40400001, "file or directory not found")
            upload = str(uuid.uuid4())
            fake.uploads[upload] = {"path": path, "length": int(self.headers["X-Upyun-Multi-Length"]),
                                    "part_size": part_size, "parts": {}}
            return self.answer(204, b"", {"X-Upyun-Multi-Uuid": upload})
        upload = fake.uploads.get(self.headers.get("X-Upyun-Multi-Uuid", ""))
        if upload is None or upload["path"] != path:
            return self.error(400, 40001035, "notfound multipart uuid")
        if stage == "upload":
            number = int(self.headers["X-Upyun-Part-Id"])
            count = -(-upload["length"] // upload["part_size"])
            expected = upload["part_size"] if number < count - 1 else upload["length"] - upload["part_size"] * (count - 1)
            if not 0 <= number < count or len(body) != expected:
                return self.error(400, 40001034, "unexpect multipart length")
            upload["parts"][number] = body
            return self.answer(204, b"", {"X-Upyun-Multi-Uuid": self.headers["X-Upyun-Multi-Uuid"]})
        if stage == "complete":
            data = b"".join(upload["parts"][number] for number in sorted(upload["parts"]))
            if len(data) != upload["length"]:
                return self.error(400, 40001037, "incomplete multipart parts")
            existed = path in fake.files
            fake.files[path] = data
            fake.dates[path] = fake.now()
            del fake.uploads[self.headers["X-Upyun-Multi-Uuid"]]
            return self.answer(204 if existed else 201)
        return self.error(400, 40001022, "wrong multipart action")

    def form_upload(self, body):
        fake = self.fake
        message = BytesParser(policy=email_policy.HTTP).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
        fields, upload = {}, None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                upload = part.get_payload(decode=True)
            else:
                fields[name] = part.get_payload(decode=True).decode()
        if "policy" not in fields:
            return self.error(400, 40000007, "need policy")
        if upload is None:
            return self.error(400, 40000005, "need file")
        policy = json.loads(base64.b64decode(fields["policy"]))
        date = policy.get("date", "")
        expected = f"UPYUN {fake.operator}:{fake.sign(f'POST&/{fake.bucket}&{date}&{fields['policy']}')}"
        if fields.get("authorization") != expected:
            return self.error(401, 40100005, "signature error")
        if policy.get("bucket") != fake.bucket:
            return self.error(400, 40000036, "uri bucket must be same as param bucket")
        if int(policy.get("expiration", 0)) < fake.now():
            return self.error(403, 40300028, "request has expired")
        low, _, high = str(policy.get("content-length-range", "0,99999999999")).partition(",")
        if not int(low) <= len(upload) <= int(high):
            return self.error(403, 40300035, "content-length of policy format error")
        path = policy["save-key"]
        with fake.lock:
            if path in fake.folders:
                return self.error(403, 40300015, "path is not a file, maybe a directory")
            fake.make_parents(path)
            fake.files[path] = upload
            fake.dates[path] = fake.now()
        return self.answer_json(200, {"code": 200, "message": "ok", "url": path, "time": fake.now()})


class CdnHandler(Handler):
    """The domain bound to the service: GET /<path>, with the token anti-leech check."""

    def do_GET(self):
        fake = self.fake
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = parse_qs(parts.query)
        if fake.token:
            value = (query.get("_upt") or [""])[0]
            sign, expires_at = value[:8], value[8:]
            good = hashlib.md5(f"{fake.token}&{expires_at}&{path}".encode()).hexdigest()[12:20]
            if not expires_at.isdigit() or int(expires_at) < fake.now() or sign != good:
                return self.answer(403, b"forbidden")
        with fake.lock:
            data = fake.files.get(path)
        if data is None:
            return self.answer(404, b"not found")
        headers = {"Content-Type": "application/octet-stream"}
        if query.get("_upd"):
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(query['_upd'][0], safe='')}"
        return self.answer(200, data, headers)
