"""An in-memory fake of a CloudFlare ImgBed site's HTTP API (apps.storages.drivers.cf_imgbed), on FakeServer, and of
the Hugging Face storage its direct uploads go to (a second server, so another origin).

    fake = FakeImgBed().start()
    token = fake.add_token("list", "upload", "delete", "manage")
    fake.url
    fake.close()

It answers as ImgBed v2.7.8's documentation (https://cfbed.sanyue.de/en/api/) and source (functions/ in
github.com/MarSeventh/CloudFlare-ImgBed) say:

- A file is a record under its id ("a/b.png"): its bytes, wherever its channel keeps them, and its metadata
  (FileName, FileType, FileSize in MB, FileSizeBytes, TimeStamp in ms, Directory "a/", Channel, ChannelName,
  ListType, Label).
- /api/manage/* needs a token with the permission of the path (delete for delete/, list for list, manage for the
  rest) in Authorization ("Bearer <token>" or the token alone), else 401 "You need to login"; the upload routes need
  upload, else 401 "Unauthorized".
- GET /api/manage/list is readIndex: dir cleaned as list.js does; the files whose Directory is dir (all below it with
  recursive), newest first, paged by start and count (-1: all); directories: the subfolders of dir with files below
  them; search keeps the files whose FileName or id has the term, ignoring case, and so narrows directories too.
  `index_down`: ImgBed's answer when it can't read its index: the folder's own files only, isIndexedResponse false.
- /api/manage/delete/<id> (any method) deletes a record, there or not: {"success", "fileId", "existed"}; folder=true
  walks the folder as delete/[[path]].js does (lines 31-88): each folder is listed through
  /api/manage/list?count=-1&dir=<id> with the id pasted into the URL as it is, so "#" ends the query, "&" the value,
  "+" is a space and "%XX" is decoded again, and list.js then cleans dir; every file listed is deleted, every folder
  listed is walked, and what couldn't be deleted (`undeletable`) is listed in "failed".
- POST /api/manage/rename/<id> {"newFileId"}: the new id cleaned as sanitizeUploadFolder does; 404, 409; a file of
  the old Telegram channel or of none (Telegraph) is 400.
- POST /upload: the file in the multipart part "file", named as uploadNameType says (origin: its own name, cleaned as
  sanitizeFileName does; else a number before it) in uploadFolder; a taken id gets "(1)". Telegram recompresses an
  image unless serverCompress=false (a shorter file; FileSizeBytes stays). initChunked, chunked, merge and cleanup as
  chunkUpload.js and chunkMerge.js: the chunk `fail_chunk` names is answered as received but not stored, and the
  merge then fails and drops the session; R2 and S3 fix the id at chunk 0, the others at the merge; WebDAV refuses.
- Hugging Face's direct upload: getUploadUrl answers an upload URL with a header its PUT must carry or, with
  `lfs_part_size`, part URLs and a completion URL: ImgBed's completeMultipart, which relays it, or with `relay` off
  (before v2.7.8) Hugging Face's own; alreadyExists when Hugging Face has the bytes. Hugging Face's storage refuses
  any Authorization header (S3: one auth mechanism only); commitUpload needs the bytes of its sha256 there.
- GET /file/<id>: no token; Range (206); 404; a blocked file (ListType Block, Label adult, or not White in
  `whitelist_mode`) is a 403 picture, or with `block_pictures` off a redirect to /blockimg or /whiteliston; a file of
  the External channel redirects to its ExternalLink.
- `body_limit`: Cloudflare's 413 for larger requests; `challenge`: Cloudflare's bot check, a 403 page for file links.
  Other paths under /api are 404, the rest the web UI's page.

Records: calls and hf_calls (method, path, query, authorization, json, form fields, the file part's name and size).
"""

from __future__ import annotations

import dataclasses
import email.utils
import hashlib
import itertools
import json
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any

from apps.storages.tests.fake_cloud_http import FakeServer, HTTPError, Request, Response, file_response, json_response

# sanitizeFileName's characters, and sanitizeUploadFolder's (each segment) without "/".
NAME_UNSAFE = re.compile(r"[\\/:*?\"'<>| ()\[\]{}#%^`~;@&=+$,]")
SEGMENT_UNSAFE = re.compile(r"[\\:*?\"'<>| ()\[\]{}#%^`~;@&=+$,]")
CHANNELS = {"telegram": "TelegramNew", "cfr2": "CloudflareR2", "s3": "S3", "discord": "Discord",
            "huggingface": "HuggingFace", "webdav": "WebDAV"}
PICTURE = b"\x89PNG\r\n\x1a\n blocked"
TEXT = {"Content-Type": "text/plain;charset=UTF-8"}


def js_decode(text: str) -> str:
    """decodeURIComponent: a malformed escape is an error (URIError), as in JS."""
    if re.search(r"%(?![0-9a-fA-F]{2})", text):
        raise ValueError("URIError: URI malformed")
    try:
        return urllib.parse.unquote(text, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("URIError: URI malformed") from None


def js_int(value: Any) -> int | None:
    """parseInt(value, 10); None for NaN."""
    match = re.match(r"\s*([+-]?[0-9]+)", value if isinstance(value, str) else "")
    return int(match.group(1)) if match else None


def sanitize_name(name: str) -> str:
    """sanitizeFileName."""
    return NAME_UNSAFE.sub("_", js_decode(name).split("/")[-1])


def sanitize_folder(folder: str) -> str:
    """sanitizeUploadFolder."""
    if not folder or not folder.strip():
        return ""
    if re.search(r"%[0-9a-fA-F]{2}", folder):
        try:
            folder = js_decode(folder)
        except ValueError:
            pass
    folder = folder.replace("..", "_")
    folder = "/".join("_" if part == "." else part for part in folder.split("/"))
    folder = re.sub(r"/{2,}", "/", folder.replace("\\", "/")).strip("/")
    return "/".join(part for part in (SEGMENT_UNSAFE.sub("_", part) for part in folder.split("/")) if part)


def route_id(raw: str) -> str:
    """params.path of a [[path]] route as ImgBed reads it: decoded, and "," as "/"."""
    return js_decode(raw).replace(",", "/")


def multipart(body: bytes, content_type: str) -> tuple[dict[str, str], tuple[str, str, bytes] | None]:
    """The fields of a multipart/form-data body, and its file part: (name, type, bytes)."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type)
    if not boundary:
        raise HTTPError(500, b"TypeError: Could not parse content as FormData.", TEXT)
    fields: dict[str, str] = {}
    file = None
    for part in body.split(b"--" + boundary.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, value = part[2:].partition(b"\r\n\r\n")
        value = value[:-2] if value.endswith(b"\r\n") else value
        disposition = head.decode("utf-8")
        name = re.search(r' name="([^"]*)"', disposition)
        filename = re.search(r'filename="([^"]*)"', disposition)
        kind = re.search(r"Content-Type:\s*([^\r\n]+)", disposition, re.IGNORECASE)
        if filename:
            file = (filename.group(1), kind.group(1).strip() if kind else "", value)
        elif name:
            fields[name.group(1)] = value.decode("utf-8")
    return fields, file


def text(status: int, message: str) -> Response:
    return Response(status, message.encode(), TEXT)


@dataclasses.dataclass
class Record:
    data: bytes
    metadata: dict[str, Any]


class FakeImgBed:
    def __init__(self):
        self.server = FakeServer(self.handle)
        self.hf = FakeServer(self.handle_hf)
        self.lock = threading.RLock()
        self.reset()

    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> FakeImgBed:
        self.server.start()
        self.hf.start()
        return self

    def close(self) -> None:
        self.server.close()
        self.hf.close()

    def reset(self) -> None:
        with self.lock:
            self.tokens: dict[str, set[str]] = {}
            self.records: dict[str, Record] = {}
            self.sessions: dict[str, dict[str, Any]] = {}
            self.slots: dict[str, dict[str, Any]] = {}  # Hugging Face's upload URLs
            self.lfs: dict[str, bytes] = {}  # what Hugging Face has, by SHA-256
            self.external: dict[str, bytes] = {}  # the links of the External channel's files, on this server
            self.calls: list[dict[str, Any]] = []
            self.hf_calls: list[dict[str, Any]] = []
            self.undeletable: set[str] = set()
            self.fail_chunk: int | None = None
            self.lfs_part_size: int | None = None
            self.relay = True
            self.index_down = False
            self.whitelist_mode = False
            self.block_pictures = True
            self.body_limit: int | None = None
            self.challenge = False
            self.clock = itertools.count(int(time.time() * 1000))  # TimeStamps, each later than the one before

    # -- for tests

    def add_token(self, *permissions: str) -> str:
        """A token made in API Token Management, with these of list, upload, delete and manage."""
        with self.lock:
            token = "imgbed_" + secrets.token_urlsafe(24)
            self.tokens[token] = set(permissions)
            return token

    def put(self, file_id: str, data: bytes, **metadata: Any) -> Record:
        """A file as ImgBed's own pages would store it (any id), its metadata changed by `metadata`."""
        with self.lock:
            folder, _, name = file_id.rpartition("/")
            record = Record(data, self._metadata(name, "application/octet-stream", len(data), folder, "TelegramNew", "tg"))
            record.metadata.update(metadata)
            self.records[file_id] = record
            return record

    def ids(self) -> list[str]:
        with self.lock:
            return sorted(self.records)

    def requests(self, method: str, route: str) -> list[dict[str, Any]]:
        """The calls of `method` to ImgBed whose decoded path matches the regular expression `route`."""
        with self.lock:
            return [call for call in self.calls if call["method"] == method and re.fullmatch(route, call["path"])]

    # -- records

    def _metadata(self, name: str, mime: str, size: int, folder: str, channel: str, channel_name: str) -> dict[str, Any]:
        return {"FileName": name, "FileType": mime, "FileSize": f"{size / 1024 / 1024:.2f}", "FileSizeBytes": size,
                "UploadIP": "127.0.0.1", "UploadAddress": "未知", "ListType": "None", "TimeStamp": next(self.clock),
                "Label": "None", "Directory": folder + "/" if folder else "", "Tags": [], "Channel": channel,
                "ChannelName": channel_name}

    @staticmethod
    def _folder_of(file_id: str, record: Record) -> str:
        return record.metadata.get("Directory") or (file_id[: file_id.rfind("/") + 1] if "/" in file_id else "")

    def _unique_id(self, args: dict[str, str], name: str) -> str:
        """buildUniqueFileId."""
        name_type = args.get("uploadNameType") or "default"
        folder = sanitize_folder(args.get("uploadFolder") or "")
        name = sanitize_name(name)
        index = int(time.time() * 1000) + secrets.randbelow(10000)
        base = name if name_type == "origin" else f"{index}_{name}"
        full = f"{folder}/{base}" if folder else base
        cut = base.rfind(".")
        stem, ext = (base[:cut], base[cut:]) if cut >= 0 else ("", base)  # JS substring(0, -1) and substring(-1)
        counter = 1
        while full in self.records:
            full = f"{folder}/{stem}({counter}){ext}" if folder else f"{stem}({counter}){ext}"
            counter += 1
        return full

    def _uploaded(self, req: Request, file_id: str) -> Response:
        result = {"src": f"/file/{file_id}" if req.arg("returnFormat") != "full" else f"{self.url}/file/{file_id}"}
        return json_response(result if req.arg("responseFormat") == "object" else [result])

    # -- requests

    def handle(self, req: Request) -> Response:
        with self.lock:
            self.calls.append(self._call_of(req))
            if self.body_limit is not None and len(req.body) > self.body_limit:
                return Response(413, b"<html>413 Request Entity Too Large</html>", {"Content-Type": "text/html"})
            raw = req.path
            if raw.startswith("/file/"):
                if self.challenge:
                    return Response(403, b"<!doctype html><title>Just a moment...</title>", {"Content-Type": "text/html"})
                return self._file(req, raw[len("/file/"):])
            if raw.startswith("/external/"):
                return file_response(self.external[urllib.parse.unquote(raw[len("/external/"):])], req.header("Range"))
            routes = (("/api/manage/list", "list", self._list), ("/api/manage/delete/", "delete", self._delete),
                      ("/api/manage/rename/", "manage", self._rename))
            for route, permission, handler in routes:
                if raw == route or (route.endswith("/") and raw.startswith(route) and len(raw) > len(route)):
                    if not self._allowed(req, permission):
                        return text(401, "You need to login")
                    return handler(req, raw[len(route):])
            uploads = {"/upload": self._upload, "/upload/huggingface/getUploadUrl": self._get_upload_url,
                       "/upload/huggingface/completeMultipart": self._complete_multipart,
                       "/upload/huggingface/commitUpload": self._commit}
            if raw in uploads:
                if not self._allowed(req, "upload"):
                    return text(401, "Unauthorized")
                return uploads[raw](req)
            if raw.startswith("/api/"):
                return text(404, "Not Found")
            return Response(200, b"<!doctype html><title>ImgBed</title>", {"Content-Type": "text/html; charset=utf-8"})

    def _call_of(self, req: Request) -> dict[str, Any]:
        content_type = req.header("Content-Type") or ""
        fields, file = (multipart(req.body, content_type) if content_type.startswith("multipart/form-data") else ({}, None))
        body = None
        if content_type.startswith("application/json") or content_type.startswith("application/vnd.git-lfs+json"):
            try:
                body = json.loads(req.body)
            except ValueError:
                pass
        return {"method": req.method, "path": urllib.parse.unquote(req.path), "target": req.target, "query": req.query,
                "authorization": req.header("Authorization"), "content_length": req.header("Content-Length"),
                "transfer_encoding": req.header("Transfer-Encoding"), "range": req.header("Range"), "json": body,
                "fields": fields, "file_name": file[0] if file else None, "file_type": file[1] if file else None,
                "file_size": len(file[2]) if file else None}

    def _allowed(self, req: Request, permission: str) -> bool:
        value = req.header("Authorization") or ""
        token = value[7:] if value.startswith("Bearer ") else value
        return permission in self.tokens.get(token, set()) if token else False

    @staticmethod
    def _clean_dir(folder: str) -> str:
        """list.js's dir (lines 55-64): ".." made "_", "\\" made "/", no "/" at the start, one at the end."""
        if folder:
            folder = re.sub(r"/{2,}", "/", folder.replace("..", "_").replace("\\", "/"))
        folder = folder[1:] if folder.startswith("/") else folder
        return folder + "/" if folder and not folder.endswith("/") else folder

    def _subfolders(self, folder: str) -> list[str]:
        """readIndex's directories of `folder` ("" or "a/b/"): the folders with files below it, as "a/b/c"."""
        found: list[str] = []
        for file_id, record in self.records.items():
            at = self._folder_of(file_id, record)
            if at and at.startswith(folder) and "/" in at[len(folder):]:
                sub = folder + at[len(folder):].split("/")[0]
                if sub not in found:
                    found.append(sub)
        return found

    def _list(self, req: Request, _: str) -> Response:
        folder = self._clean_dir(req.arg("dir", "") or "")
        count = js_int(req.arg("count")) or 50
        start = js_int(req.arg("start")) or 0
        search = req.arg("search", "") or ""
        if search:
            try:
                search = js_decode(search).strip()
            except ValueError as error:
                return text(500, f"{error}")
        if self.index_down:  # getAllFileRecords: a prefix of the ids, the folder's own files, no pages
            found = [(file_id, record) for file_id, record in self.records.items() if file_id.startswith(folder)]
            direct = [(file_id, record) for file_id, record in found if "/" not in file_id[len(folder):]]
            subfolders = sorted({folder + file_id[len(folder):].split("/")[0] for file_id, _ in found if "/" in file_id[len(folder):]})
            return json_response({"files": [{"name": i, "metadata": dict(r.metadata)} for i, r in direct],
                                  "directories": subfolders, "isIndexedResponse": False})
        files = sorted(self.records.items(), key=lambda item: -float(item[1].metadata.get("TimeStamp") or 0))
        found = [(i, r) for i, r in files if not folder or self._folder_of(i, r).startswith(folder)]
        if search:
            found = [(i, r) for i, r in found
                     if search.lower() in str(r.metadata.get("FileName") or "").lower() or search.lower() in i.lower()]
        direct = [(i, r) for i, r in found if self._folder_of(i, r) == folder]
        result = found if req.arg("recursive") == "true" else direct
        if count != -1:
            result = result[max(0, start): max(0, start) + max(1, count)]
        subfolders: list[str] = []
        for i, r in found:
            at = self._folder_of(i, r)
            if at and at.startswith(folder) and "/" in at[len(folder):]:
                sub = folder + at[len(folder):].split("/")[0]
                if sub not in subfolders:
                    subfolders.append(sub)
        return json_response({"files": [{"name": i, "metadata": dict(r.metadata)} for i, r in result],
                              "directories": subfolders, "totalCount": len(found), "directFileCount": len(direct),
                              "directFolderCount": len(subfolders), "returnedCount": len(result),
                              "indexLastUpdated": int(time.time() * 1000), "isIndexedResponse": True})

    def _delete(self, req: Request, raw: str) -> Response:
        try:
            file_id = route_id(raw)
        except ValueError as error:
            return json_response({"success": False, "error": str(error)}, 400)
        if req.arg("folder") == "true":
            return self._delete_folder(file_id)
        if file_id in self.undeletable:
            return json_response({"success": False, "error": "Delete file failed"}, 400)
        existed = self.records.pop(file_id, None) is not None
        return json_response({"success": True, "fileId": file_id, "existed": existed})

    def _delete_folder(self, folder: str) -> Response:
        """delete/[[path]].js's folder delete: each folder listed through a URL with its id pasted in,
        new URL(`${url.origin}/api/manage/list?count=-1&dir=${currentFolder.path}`) (line 46), read back with
        searchParams.get('dir') and cleaned by list.js; its files deleted, its folders walked next."""
        queue, walked, deleted, failed = [folder], set(), [], []
        while queue:
            query = urllib.parse.urlsplit(f"http://imgbed.invalid/api/manage/list?count=-1&dir={queue.pop(0)}").query
            listed = self._clean_dir(urllib.parse.parse_qs(query, keep_blank_values=True).get("dir", [""])[0])
            if listed in walked:
                continue
            walked.add(listed)
            queue += self._subfolders(listed)
            for file_id in [i for i, r in self.records.items() if self._folder_of(i, r) == listed]:
                if file_id in self.undeletable:
                    failed.append(file_id)
                else:
                    del self.records[file_id]
                    deleted.append(file_id)
        return json_response({"success": True, "deleted": deleted, "failed": failed})

    def _rename(self, req: Request, raw: str) -> Response:
        if req.method != "POST":
            return json_response({"success": False, "message": "Method not allowed. Use POST."}, 405)
        file_id = route_id(raw)
        try:
            body = json.loads(req.body)
        except ValueError:
            return json_response({"success": False, "message": "Invalid request body. Expected JSON."}, 400)
        new = body.get("newFileId") if isinstance(body, dict) else None
        if not isinstance(new, str) or not new.strip():
            return json_response({"success": False, "message": "newFileId is required and must be a non-empty string."}, 400)
        new = sanitize_folder(new.strip())
        record = self.records.get(file_id)
        if record is None:
            return json_response({"success": False, "message": "File not found."}, 404)
        if new in self.records:
            return json_response({"success": False, "message": "目标文件名已存在"}, 409)
        if record.metadata.get("Channel") in ("Telegram", None):
            return json_response({"success": False, "message": "旧版 Telegram/Telegraph 渠道不支持重命名"}, 400)
        record.metadata["Directory"] = new.rpartition("/")[0] + "/" if "/" in new else ""
        self.records[new] = self.records.pop(file_id)
        return json_response({"success": True, "newFileId": new, "metadata": dict(record.metadata)})

    # -- uploads

    def _form(self, req: Request) -> tuple[dict[str, str], tuple[str, str, bytes] | None]:
        """request.formData(): multipart or urlencoded, else an error (a 500, through the error middleware)."""
        content_type = req.header("Content-Type") or ""
        if content_type.startswith("multipart/form-data"):
            return multipart(req.body, content_type)
        if content_type.startswith("application/x-www-form-urlencoded"):
            return req.form(), None
        raise HTTPError(500, b"TypeError: Could not parse content as FormData.", TEXT)

    def _upload(self, req: Request) -> Response:
        if req.arg("cleanup") == "true":
            self.sessions.pop(req.arg("uploadId") or "", None)
            return json_response({"success": True, "uploadId": req.arg("uploadId"), "cleanedChunks": js_int(req.arg("totalChunks")) or 0})
        fields, file = self._form(req)
        if req.arg("initChunked") == "true":
            return self._init_chunked(req, fields)
        if req.arg("chunked") == "true":
            return self._merge(req, fields) if req.arg("merge") == "true" else self._chunk(req, fields, file)
        if file is None:
            return text(500, "TypeError: Cannot read properties of null (reading 'name')")
        name, mime, data = file
        channel = CHANNELS.get(req.arg("uploadChannel") or "", "TelegramNew")
        try:
            file_id = self._unique_id({key: req.arg(key) or "" for key in ("uploadNameType", "uploadFolder")}, name)
        except ValueError as error:
            return text(500, f"{error}")
        stored = data
        if channel == "TelegramNew" and mime.startswith("image/") and req.arg("serverCompress") != "false":
            stored = data[: len(data) // 2]  # sendPhoto: Telegram recompresses it
        metadata = self._metadata(name, mime or "application/octet-stream", len(data),
                                  sanitize_folder(req.arg("uploadFolder") or ""), channel, req.arg("channelName") or "default")
        metadata["FileSize"] = f"{len(stored) / 1024 / 1024:.2f}"
        self.records[file_id] = Record(stored, metadata)
        return self._uploaded(req, file_id)

    def _init_chunked(self, req: Request, fields: dict[str, str]) -> Response:
        name, total = fields.get("originalFileName"), js_int(fields.get("totalChunks"))
        mime = fields.get("originalFileType") or "application/octet-stream"
        if not name or not total:
            return text(400, "Error: Missing initialization parameters")
        channel = req.arg("uploadChannel") or "telegram"
        if channel == "webdav":
            return text(400, "Error: WebDAV channel does not support chunked uploads. Please use non-chunked upload within "
                             "your Cloudflare request body limit.")
        upload_id = f"upload_{int(time.time() * 1000)}_{secrets.token_hex(5)}"
        self.sessions[upload_id] = {"name": name, "type": mime, "total": total, "channel": channel, "chunks": {}, "key": None}
        return json_response({"success": True, "uploadId": upload_id, "message": "Chunked upload initialized successfully",
                              "sessionInfo": {"uploadId": upload_id, "originalFileName": name, "totalChunks": total,
                                              "uploadChannel": channel, "channelName": req.arg("channelName") or ""}})

    def _session(self, req: Request, fields: dict[str, str]) -> tuple[dict[str, Any], str] | Response:
        upload_id, total, name = fields.get("uploadId"), js_int(fields.get("totalChunks")), fields.get("originalFileName")
        session = self.sessions.get(upload_id or "")
        if session is None:
            return text(400, "Error: Invalid or expired upload session")
        if session["name"] != name or session["total"] != total:
            return text(400, "Error: Session parameters mismatch")
        channel = req.arg("uploadChannel") or session["channel"]
        if channel == "webdav":
            return text(400, "Error: WebDAV channel does not support chunked uploads.")
        return session, channel

    def _chunk(self, req: Request, fields: dict[str, str], file: tuple[str, str, bytes] | None) -> Response:
        index = js_int(fields.get("chunkIndex"))
        if file is None or index is None or not fields.get("uploadId") or not fields.get("originalFileName"):
            return text(400, "Error: Missing chunk upload parameters")
        found = self._session(req, fields)
        if isinstance(found, Response):
            return found
        session, channel = found
        if channel in ("cfr2", "s3") and index == 0:  # the multipart upload's key, the file's id
            session["key"] = self._unique_id({key: req.arg(key) or "" for key in ("uploadNameType", "uploadFolder")}, session["name"])
        if self.fail_chunk != index:  # a failed chunk is answered all the same, and the merge finds it missing
            session["chunks"][index] = file[2]
        return json_response({"success": True, "message": f"Chunk {index + 1}/{session['total']} received and being uploaded",
                              "uploadId": fields["uploadId"], "chunkIndex": index})

    def _merge(self, req: Request, fields: dict[str, str]) -> Response:
        if not fields.get("uploadId") or not js_int(fields.get("totalChunks")) or not fields.get("originalFileName"):
            return text(400, "Error: Missing merge parameters")
        found = self._session(req, fields)
        if isinstance(found, Response):
            return found
        session, channel = found
        self.sessions.pop(fields["uploadId"])  # after the merge, or its failure
        total = session["total"]
        if sorted(session["chunks"]) != list(range(total)):
            return text(500, f"Error: Failed to merge chunks - Only {len(session['chunks'])}/{total} chunks completed successfully.")
        if channel in ("cfr2", "s3"):
            file_id = session["key"]
            if file_id is None:
                return text(500, "Error: Failed to merge chunks - Multipart upload info not found")
        else:
            file_id = self._unique_id({key: req.arg(key) or "" for key in ("uploadNameType", "uploadFolder")}, session["name"])
        data = b"".join(session["chunks"][index] for index in range(total))
        metadata = self._metadata(session["name"], session["type"], len(data), sanitize_folder(req.arg("uploadFolder") or ""),
                                  CHANNELS.get(channel, "TelegramNew"), req.arg("channelName") or "default")
        metadata["IsChunked"] = True
        self.records[file_id] = Record(data, metadata)
        return self._uploaded(req, file_id)

    # -- Hugging Face's direct upload

    @staticmethod
    def _json(req: Request) -> dict[str, Any]:
        try:
            body = json.loads(req.body)
        except ValueError:
            raise HTTPError(500, {"error": "Unexpected end of JSON input"}) from None
        if not isinstance(body, dict):
            raise HTTPError(500, {"error": "body is not an object"})
        return body

    def _get_upload_url(self, req: Request) -> Response:
        body = self._json(req)
        size, name, sha, sample = body.get("fileSize"), body.get("fileName"), body.get("sha256"), body.get("fileSample")
        if not size or not name or not sha or not sample:
            return json_response({"error": "Missing required fields: fileSize, fileName, sha256, fileSample"}, 400)
        full = self._unique_id({key: body.get(key) or "" for key in ("uploadNameType", "uploadFolder")}, name)
        folder, _, last = full.rpartition("/")
        path = f"{folder}/{secrets.token_hex(8)}_{last}" if folder else f"{secrets.token_hex(8)}_{last}"
        info = {"success": True, "fullId": full, "filePath": path, "channelName": body.get("channelName") or "hf",
                "repo": "someone/imgbed", "isPrivate": False, "oid": sha}
        if sha in self.lfs:
            return json_response({**info, "needsLfs": True, "alreadyExists": True})
        slot = secrets.token_hex(8)
        self.slots[slot] = {"oid": sha, "size": size, "parts": {}}
        if self.lfs_part_size:
            header = {"chunk_size": str(self.lfs_part_size),
                      **{f"{n:05d}": f"{self.hf.url}/lfs/{slot}/{n}" for n in range(1, -(-size // self.lfs_part_size) + 1)}}
            completion = f"https://huggingface.co/api/complete_multipart?uploadId={slot}"
            href = (f"{self.url}/upload/huggingface/completeMultipart?target={urllib.parse.quote(completion, safe='')}"
                    if self.relay else f"{self.hf.url}/api/complete_multipart?uploadId={slot}")
        else:
            header, href = {"X-Upload-Check": slot}, f"{self.hf.url}/lfs/{slot}"
        return json_response({**info, "needsLfs": True, "alreadyExists": False, "uploadAction": {"href": href, "header": header}})

    def _complete_multipart(self, req: Request) -> Response:
        target = urllib.parse.urlsplit(req.arg("target") or "")
        if (target.scheme, target.hostname, target.path) != ("https", "huggingface.co", "/api/complete_multipart"):
            return json_response({"error": "Invalid multipart completion target"}, 400)
        return self._complete((urllib.parse.parse_qs(target.query).get("uploadId") or [""])[0], req.body)

    def _complete(self, slot_id: str, body: bytes) -> Response:
        slot = self.slots.get(slot_id)
        try:
            asked = json.loads(body)
        except ValueError:
            asked = None
        if slot is None or not isinstance(asked, dict) or asked.get("oid") != slot["oid"] or not isinstance(asked.get("parts"), list):
            return json_response({"error": "Invalid multipart completion payload"}, 400)
        numbers = []
        for part in asked["parts"]:
            number = part.get("partNumber") if isinstance(part, dict) else None
            data = slot["parts"].get(number)
            if data is None or part.get("etag") != f'"{hashlib.md5(data).hexdigest()}"':
                return json_response({"error": "InvalidPart"}, 400)
            numbers.append(number)
        if numbers != sorted(slot["parts"]):
            return json_response({"error": "InvalidPartOrder"}, 400)
        self._keep(slot, b"".join(slot["parts"][number] for number in numbers))
        return json_response({"success": True})

    def _keep(self, slot: dict[str, Any], data: bytes) -> None:
        """Hugging Face keeps the bytes under their SHA-256, when they are those of the upload's oid."""
        if hashlib.sha256(data).hexdigest() == slot["oid"] and len(data) == slot["size"]:
            self.lfs[slot["oid"]] = data

    def _commit(self, req: Request) -> Response:
        body = self._json(req)
        full, path, sha, size = body.get("fullId"), body.get("filePath"), body.get("sha256"), body.get("fileSize")
        if not full or not path or not sha or not size:
            return json_response({"error": "Missing required fields: fullId, filePath, sha256, fileSize"}, 400)
        if sanitize_folder(full) != full:
            return json_response({"error": "Invalid fullId: contains illegal path characters"}, 400)
        data = self.lfs.get(sha)
        if data is None or len(data) != size:
            return json_response({"error": "Commit failed: 400 - LFS object not found"}, 500)
        folder, _, name = full.rpartition("/")
        metadata = self._metadata(body.get("fileName") or full, body.get("fileType") or "application/octet-stream", size, folder,
                                  "HuggingFace", body.get("channelName") or "hf")
        metadata["HfFilePath"] = path
        self.records[full] = Record(data, metadata)
        return json_response({"success": True, "src": f"/file/{full}", "fullId": full,
                              "fileUrl": f"https://huggingface.co/datasets/someone/imgbed/resolve/main/{path}"})

    def handle_hf(self, req: Request) -> Response:
        """Hugging Face's storage: presigned URLs, which take no other credentials."""
        with self.lock:
            self.hf_calls.append(self._call_of(req))
            if req.header("Authorization"):
                return Response(400, b"<Error><Code>InvalidArgument</Code><Message>Only one auth mechanism allowed</Message></Error>",
                                {"Content-Type": "application/xml"})
            if req.method == "POST" and req.path == "/api/complete_multipart":
                return self._complete(req.arg("uploadId") or "", req.body)
            match = re.fullmatch(r"/lfs/([0-9a-f]+)(?:/([0-9]+))?", req.path)
            slot = self.slots.get(match.group(1)) if match and req.method == "PUT" else None
            if slot is None:
                return Response(403, b"<Error><Code>AccessDenied</Code></Error>", {"Content-Type": "application/xml"})
            if match.group(2) is None:
                if req.header("X-Upload-Check") != match.group(1):
                    return Response(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>", {"Content-Type": "application/xml"})
                self._keep(slot, req.body)
                return Response(200, b"")
            slot["parts"][int(match.group(2))] = req.body
            return Response(200, b"", {"ETag": f'"{hashlib.md5(req.body).hexdigest()}"'})

    # -- downloads

    def _file(self, req: Request, raw: str) -> Response:
        try:
            file_id = route_id(raw)
        except ValueError:
            return text(400, "Error: Decode Image ID Failed")
        record = self.records.get(file_id)
        if record is None:
            return text(404, "Error: Image Not Found")
        meta = record.metadata
        if meta.get("ListType") != "White" and (meta.get("ListType") == "Block" or meta.get("Label") == "adult" or self.whitelist_mode):
            if self.block_pictures:
                return Response(403, PICTURE, {"Content-Type": "image/png", "Content-Disposition": "inline"})
            page = "/blockimg" if meta.get("ListType") == "Block" or meta.get("Label") == "adult" else "/whiteliston"
            return Response(302, b"", {"Location": self.url + page})
        if meta.get("Channel") == "External":
            return Response(302, b"", {"Location": meta["ExternalLink"]})
        name = urllib.parse.quote(str(meta.get("FileName") or file_id))
        headers = {"Content-Type": meta.get("FileType") or "application/octet-stream",
                   "Content-Disposition": f"inline; filename=\"{name}\"; filename*=UTF-8''{name}",
                   "Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=2592000",
                   "Last-Modified": email.utils.formatdate(usegmt=True)}
        return file_response(record.data, req.header("Range"), headers)
