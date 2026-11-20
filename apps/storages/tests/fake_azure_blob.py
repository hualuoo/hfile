"""An in-memory fake of one Azure storage account's Blob service REST API (tests only, no Django).

    fake = FakeAzureBlob().start()         # fake.url, fake.account, fake.key (the account key, Base64)
    fake.add_container("files")            # public=True: anonymous reads of its blobs
    fake.close()

Written from Microsoft's REST docs (their URLs are in apps/storages/drivers/azure_blob.py), and on purpose
without any code of the driver: it signs nothing, it checks.
- Shared Key: the string to sign of "Authorize with Shared Key" rebuilt from the request as it arrived (the
  standard headers in their order, Content-Length empty when zero, the x-ms-* headers canonicalised with the
  whitespace rule, the encoded path, the decoded query); x-ms-date within 15 minutes; x-ms-version present; a
  header sent twice is a 400.
- Service SAS (sv 2020-12-06 and later): the signature over its 16 fields, the expiry, sr=b, sp in its fixed
  order with r for reads; rscd, rsct, ... set the answer's headers. Without either, only blobs of public
  containers can be read (else 404 ResourceNotFound, as Azure answers).
- List Blobs: prefix, delimiter, maxresults, include=metadata, opaque NextMarker; names with characters XML
  can't carry come percent-encoded with Encoded="true".
- Get Blob (Range), Get Blob Properties (errors with no body, the code in x-ms-error-code), Put Blob
  (x-ms-blob-type and Content-Length required, Content-MD5 computed and sent back), Put Block (Base64 ids of at
  most 64 bytes, of one length per blob), Put Block List, Copy Blob (source URL of this account, at most 2 KiB),
  Abort Copy Blob, Delete Blob (SnapshotsPresent).
- Errors as <Error><Code/><Message/></Error> with x-ms-error-code; AuthenticationFailed carries the server's
  string to sign, as Azure's does. A request whose body ends before its Content-Length (the client went away)
  is dropped unanswered, and stores nothing.
Stricter than Azure on purpose: a raw "+" in a query (its meaning is ambiguous) and a name with a part ending
in "." (which Azure trims to another name) are 400s.

hns=True is an account with a hierarchical namespace (Data Lake Storage), as the docs describe it: uploads
and copies make the directories above them; directories are zero-length entries with ResourceType
"directory" and hdi_isfolder=true, listed as prefixes when a delimiter is given; Delete Blob takes a
directory only when it is empty; a prefix that runs through a file is an error; Put Block and Put Block List
can't replace a file made through the Data Lake API (BlobOperationNotSupported). As rclone found, Put Blob
of "name/" with hdi_isfolder=true makes the directory "name", and other requests to "name/" are 400
InvalidUri. Where the docs give no error code, the ones here are guesses (PathConflict, DirectoryIsNotEmpty):
the driver doesn't depend on them.

Knobs: clock_skew (seconds added to this account's clock), copy_mode ("sync", or "pending", "fail" and
"never" after copy_polls reads of the target), empty_pages (an empty page with a NextMarker before each
page), corrupt_md5, fail(status, code, method, times). requests records what was asked; put_raw() adds blobs
the way other programs would.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import email.utils
import hashlib
import hmac
import itertools
import re
import threading
import time
import types
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response

ACCOUNT = "hfiletest"
KEY = base64.b64encode(hashlib.sha512(b"hfile fake azure account key").digest()).decode()  # 64 bytes, as Azure's

# "Authorize with Shared Key": the standard headers of the string to sign, in its order.
STANDARD_HEADERS = ["Content-Encoding", "Content-Language", "Content-Length", "Content-MD5", "Content-Type", "Date",
                    "If-Modified-Since", "If-Match", "If-None-Match", "If-Unmodified-Since", "Range"]
SAS_PERMISSION_ORDER = "racwdxltmeop"
SAS_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,7})?)?Z)?")


def error(status: int, code: str, message: str = "", **details: str) -> HTTPError:
    text = (message or code) + f"\nRequestId:{uuid.uuid4()}\nTime:{datetime.datetime.now(datetime.timezone.utc).isoformat()}"
    extra = "".join(f"<{name}>{escape(value)}</{name}>" for name, value in details.items())
    body = f'<?xml version="1.0" encoding="utf-8"?><Error><Code>{code}</Code><Message>{escape(text)}</Message>{extra}</Error>'
    return HTTPError(status, Response(status, body.encode(), {"Content-Type": "application/xml",
                                                              "x-ms-error-code": code}))


def http_date(seconds: float) -> str:
    return email.utils.formatdate(seconds, usegmt=True)


def sas_time(value: str) -> datetime.datetime:
    """A SAS time (one of the ISO 8601 UTC forms SAS_TIME matches) as an aware datetime."""
    text = value if "T" in value else value + "T00:00Z"
    whole, _, fraction = text[:-1].partition(".")
    moment = datetime.datetime.fromisoformat(whole).replace(tzinfo=datetime.timezone.utc)
    return moment + datetime.timedelta(seconds=float("0." + fraction)) if fraction else moment


def canonical_value(value: str) -> str:
    """A header value as the string to sign has it: runs of linear whitespace made one space, except inside a
    quoted string, and trimmed."""
    out: list[str] = []
    quoted = gap = False
    for ch in value.strip():
        if ch == '"':
            quoted = not quoted
        if not quoted and ch in " \t\r\n":
            gap = True
            continue
        if gap:
            out.append(" ")
            gap = False
        out.append(ch)
    return "".join(out)


@dataclasses.dataclass
class Blob:
    data: bytes = b""
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = dataclasses.field(default_factory=dict)
    created: float = dataclasses.field(default_factory=time.time)
    modified: float = dataclasses.field(default_factory=time.time)
    etag: str = ""
    md5: str | None = None  # Base64, as Put Blob stores it
    kind: str = "file"  # "directory": a Data Lake directory (hns)
    snapshots: int = 0
    dfs: bool = False  # made through the Data Lake API (hns)
    blocks: list[tuple[str, bytes]] = dataclasses.field(default_factory=list)  # committed (Put Block List)
    copy: dict[str, Any] | None = None  # x-ms-copy-* of the last Copy Blob into it


class FakeAzureBlob:
    def __init__(self, *, hns: bool = False):
        self.hns = hns
        self.account = ACCOUNT
        self.key = KEY
        self.lock = threading.Lock()
        self.containers: dict[str, dict[str, Any]] = {}
        self.uncommitted: dict[tuple[str, str], dict[str, bytes]] = {}
        self.requests: list[types.SimpleNamespace] = []
        self.failures: list[list[Any]] = []
        self.clock_skew = 0.0
        self.copy_mode = "sync"
        self.copy_polls = 1
        self.empty_pages = False
        self.corrupt_md5 = False
        self._etags = itertools.count(0x8D000000)
        self.server = FakeServer(self.handle)

    def start(self) -> FakeAzureBlob:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def url(self) -> str:
        return self.server.url

    def now(self) -> float:
        return time.time() + self.clock_skew

    # -- set-up and inspection (tests)

    def reset(self) -> None:
        with self.lock:
            self.containers.clear()
            self.uncommitted.clear()
            self.requests.clear()
            self.failures.clear()
            self.clock_skew = 0.0
            self.copy_mode, self.copy_polls = "sync", 1
            self.empty_pages = self.corrupt_md5 = False

    def add_container(self, name: str, *, public: bool = False) -> None:
        with self.lock:
            self.containers[name] = {"public": public, "blobs": {}}

    def put_raw(self, container: str, name: str, data: bytes = b"", *, metadata: dict[str, str] | None = None,
                kind: str = "file", dfs: bool = False, snapshots: int = 0) -> None:
        """A blob as another program would have made it, without a request (and without the checks)."""
        with self.lock:
            blobs = self.containers[container]["blobs"]
            if self.hns:
                self._make_parents(blobs, name)
            blobs[name] = self._new_blob(data, metadata=dict(metadata or {}), kind=kind)
            blobs[name].dfs, blobs[name].snapshots = dfs, snapshots

    def names(self, container: str) -> list[str]:
        with self.lock:
            return sorted(self.containers[container]["blobs"])

    def blob(self, container: str, name: str) -> Blob | None:
        with self.lock:
            return self.containers[container]["blobs"].get(name)

    def fail(self, status: int, code: str, *, method: str | None = None, times: int = 1) -> None:
        """The next `times` requests (of `method`) get this error."""
        with self.lock:
            self.failures.append([method, status, code, times])

    def _new_blob(self, data: bytes, **fields: Any) -> Blob:
        now = time.time()
        return Blob(data=data, created=now, modified=now, etag=f'"0x{next(self._etags):X}"', **fields)

    def _directory(self) -> Blob:
        return self._new_blob(b"", kind="directory", metadata={"hdi_isfolder": "true"})

    # -- requests

    def handle(self, req) -> Response:
        length = req.header("Content-Length")
        if length is not None and length.strip().isdigit() and len(req.body) != int(length):
            return Response(0)  # the client went away halfway through the body: nothing is stored
        with self.lock:
            return self._handle(req)

    def _handle(self, req) -> Response:
        container_raw, slash, blob_raw = req.path[1:].partition("/")
        try:
            container = urllib.parse.unquote(container_raw, errors="strict")
            name = urllib.parse.unquote(blob_raw, errors="strict") if slash and blob_raw else None
        except UnicodeDecodeError:
            raise error(400, "InvalidUri", "The requested URI does not represent any resource on the server.")
        query = self._query(req.raw_query)
        self.requests.append(types.SimpleNamespace(
            method=req.method, container=container, name=name, query=query,
            headers={key.lower(): value for key, value in req.headers.items()}))
        for failure in self.failures:
            if failure[3] > 0 and failure[0] in (None, req.method):
                failure[3] -= 1
                raise error(failure[1], failure[2], "Injected failure.")
        overrides = self._authorize(req, container, name, query)
        if container not in self.containers:
            raise error(404, "ContainerNotFound", "The specified container does not exist.")
        store = self.containers[container]
        if name is None:
            if req.method == "GET" and query.get("restype") == ["container"] and query.get("comp") == ["list"]:
                return self._list(container, store["blobs"], query)
            raise error(400, "InvalidQueryParameterValue", "Only List Blobs is faked on containers.")
        self._check_name(name)
        if overrides is not None and req.method not in ("GET", "HEAD"):
            raise error(403, "AuthorizationPermissionMismatch", "This request is not authorized to perform this operation using this permission.")
        comp = (query.get("comp") or [""])[0]
        if not (req.method == "PUT" and not comp and req.header("x-ms-copy-source") is None):
            self._no_folder_name(name)  # Put Blob of "name/" can make a directory
        if req.method in ("GET", "HEAD"):
            return self._read(req, store["blobs"], name, overrides or {})
        if req.method == "PUT" and comp == "block":
            return self._put_block(req, container, store["blobs"], name, query)
        if req.method == "PUT" and comp == "blocklist":
            return self._put_block_list(req, container, store["blobs"], name)
        if req.method == "PUT" and comp == "copy":
            return self._abort_copy(req, store["blobs"], name, query)
        if req.method == "PUT" and not comp:
            if req.header("x-ms-copy-source") is not None:
                return self._copy(req, store["blobs"], name)
            return self._put_blob(req, container, store["blobs"], name)
        if req.method == "DELETE" and not comp:
            return self._delete(req, container, store["blobs"], name)
        raise error(400, "UnsupportedHttpVerb", "Not faked.")

    @staticmethod
    def _query(raw: str) -> dict[str, list[str]]:
        """{name: [values]}, decoded with unquote: a raw "+" is refused rather than read as a space or a plus."""
        values: dict[str, list[str]] = {}
        for pair in raw.split("&") if raw else []:
            if "+" in pair:
                raise error(400, "InvalidQueryParameterValue", "A raw + in the query.")
            key, _, value = pair.partition("=")
            try:
                values.setdefault(urllib.parse.unquote(key, errors="strict"), []).append(
                    urllib.parse.unquote(value, errors="strict"))
            except UnicodeDecodeError:
                raise error(400, "InvalidQueryParameterValue", "Not UTF-8.")
        return values

    def _check_name(self, name: str) -> None:
        if len(name) > 1024:
            raise error(400, "InvalidUri", "The blob name is longer than 1,024 characters.")
        if any(part.endswith(".") for part in name.rstrip("/").split("/")):
            raise error(400, "InvalidUri", "Azure would trim the dot at the end of this name (refused by the fake).")

    def _no_folder_name(self, name: str) -> None:
        """hns: a name ending with "/" addresses nothing (Delete Blob of one is 400 InvalidUri, as rclone saw)."""
        if self.hns and name.endswith("/"):
            raise error(400, "InvalidUri", "The requested URI does not represent any resource on the server.")

    # -- authorization

    def _authorize(self, req, container: str, name: str | None, query: dict[str, list[str]]) -> dict[str, str] | None:
        """None for a Shared Key request; the response headers a SAS sets; {} for an anonymous read."""
        if req.header("Authorization") is not None:
            self._check_shared_key(req, query)
            return None
        if "sig" in query:
            return self._check_sas(req, container, name, query)
        store = self.containers.get(container)
        if store is None or not store["public"] or name is None or req.method not in ("GET", "HEAD"):
            raise error(404, "ResourceNotFound", "The specified resource does not exist.")
        return {}

    def string_to_sign(self, req, query: dict[str, list[str]]) -> str:
        lines = [req.method]
        for header in STANDARD_HEADERS:
            values = req.headers.get_all(header) or []
            if len(values) > 1:
                raise error(400, "InvalidHeaderValue", f"{header} was sent twice.")
            value = values[0] if values else ""
            lines.append("" if header == "Content-Length" and value.strip() == "0" else value)
        canonical: dict[str, str] = {}
        for header, value in req.headers.items():
            lower = header.lower()
            if lower.startswith("x-ms-"):
                if lower in canonical:
                    raise error(400, "InvalidHeaderValue", f"{header} was sent twice.")
                canonical[lower] = canonical_value(value)
        resource = f"/{self.account}{req.path}"
        for key in sorted({key.lower() for key in query}):
            values = sorted(value for name, items in query.items() if name.lower() == key for value in items)
            resource += f"\n{key}:{','.join(values)}"
        return "\n".join(lines) + "\n" + "".join(f"{key}:{canonical[key]}\n" for key in sorted(canonical)) + resource

    def _check_shared_key(self, req, query: dict[str, list[str]]) -> None:
        if not req.header("x-ms-version"):
            raise error(400, "MissingRequiredHeader", "An HTTP header that's mandatory for this request is not specified.")
        sent = req.header("x-ms-date") or req.header("Date")
        try:
            moment = email.utils.parsedate_to_datetime(sent).timestamp() if sent else None
        except (TypeError, ValueError, IndexError):
            moment = None
        if moment is None or abs(self.now() - moment) > 15 * 60:
            raise error(403, "AuthenticationFailed", "Server failed to authenticate the request. Make sure the value "
                        "of Authorization header is formed correctly including the signature.",
                        AuthenticationErrorDetail=f"Request date header too old: '{sent}'")
        scheme, _, credential = (req.header("Authorization") or "").partition(" ")
        account, _, given = credential.partition(":")
        text = self.string_to_sign(req, query)
        expected = base64.b64encode(hmac.new(base64.b64decode(self.key), text.encode("utf-8"), hashlib.sha256).digest()).decode()
        if scheme != "SharedKey" or account != self.account or not hmac.compare_digest(given, expected):
            raise error(403, "AuthenticationFailed", "Server failed to authenticate the request. Make sure the value "
                        "of Authorization header is formed correctly including the signature.",
                        AuthenticationErrorDetail=f"The MAC signature found in the HTTP request '{given}' is not the "
                                                  f"same as any computed signature. Server used following string to "
                                                  f"sign: '{text}'.")

    def _check_sas(self, req, container: str, name: str | None, query: dict[str, list[str]]) -> dict[str, str]:
        def field(key: str) -> str:
            values = query.get(key)
            return values[0] if values else ""

        refused = error(403, "AuthenticationFailed", "Server failed to authenticate the request. Make sure the value "
                        "of Authorization header is formed correctly including the signature.")
        sv, sr, sp, se, st, sig = (field(key) for key in ("sv", "sr", "sp", "se", "st", "sig"))
        if not (sv and sr and sp and se and sig) or sv < "2020-12-06" or sr != "b" or name is None:
            raise refused
        order = [SAS_PERMISSION_ORDER.find(letter) for letter in sp]
        if -1 in order or order != sorted(set(order)):
            raise refused
        if req.method in ("GET", "HEAD") and "r" not in sp:
            raise error(403, "AuthorizationPermissionMismatch", "This request is not authorized to perform this operation using this permission.")
        for value in (se, st):
            if value and not SAS_TIME.fullmatch(value):
                raise refused
        now = datetime.datetime.fromtimestamp(self.now(), datetime.timezone.utc)
        if now >= sas_time(se) or (st and now < sas_time(st)):
            raise refused
        text = "\n".join([sp, st, se, f"/blob/{self.account}/{container}/{name}", field("si"), field("sip"),
                          field("spr"), sv, sr, "", field("ses"), field("rscc"), field("rscd"), field("rsce"),
                          field("rscl"), field("rsct")])
        expected = base64.b64encode(hmac.new(base64.b64decode(self.key), text.encode("utf-8"), hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(sig, expected):
            raise refused
        return {header: field(key) for key, header in (("rscc", "Cache-Control"), ("rscd", "Content-Disposition"),
                                                        ("rsce", "Content-Encoding"), ("rscl", "Content-Language"),
                                                        ("rsct", "Content-Type")) if field(key)}

    # -- reading

    def _headers(self, blob: Blob) -> dict[str, str]:
        headers = {"Content-Type": blob.content_type, "ETag": blob.etag, "Last-Modified": http_date(blob.modified),
                   "x-ms-creation-time": http_date(blob.created), "x-ms-blob-type": "BlockBlob",
                   "x-ms-lease-status": "unlocked", "x-ms-lease-state": "available", "x-ms-server-encrypted": "true",
                   "Accept-Ranges": "bytes"}
        headers.update({f"x-ms-meta-{key}": value for key, value in blob.metadata.items()})
        if self.hns:
            headers["x-ms-resource-type"] = blob.kind
        if blob.copy is not None:
            headers.update({"x-ms-copy-id": blob.copy["id"], "x-ms-copy-status": blob.copy["status"],
                            "x-ms-copy-source": blob.copy["source"], "x-ms-copy-progress": blob.copy["progress"]})
            if blob.copy.get("description"):
                headers["x-ms-copy-status-description"] = blob.copy["description"]
        return headers

    def _read(self, req, blobs: dict[str, Blob], name: str, overrides: dict[str, str]) -> Response:
        blob = blobs.get(name)
        if blob is None:
            raise error(404, "BlobNotFound", "The specified blob does not exist.")
        self._advance_copy(blob)
        headers = self._headers(blob)
        headers.update(overrides)
        if req.method == "HEAD":
            if blob.md5:
                headers["Content-MD5"] = blob.md5
            headers["Content-Length"] = str(len(blob.data))
            return Response(200, b"", headers)
        span = req.header("x-ms-range") or req.header("Range")
        if not span and blob.md5:
            headers["Content-MD5"] = blob.md5
        return file_response(blob.data, span, headers)

    def _advance_copy(self, blob: Blob) -> None:
        """A pending copy moves on each time its target is read (copy_polls reads)."""
        copy = blob.copy
        if copy is None or copy["status"] != "pending" or copy["mode"] == "never":
            return
        copy["polls"] -= 1
        if copy["polls"] > 0:
            return
        if copy["mode"] == "fail":
            copy.update(status="failed", description="500 InternalServerError \"Copy failed (fake).\"")
            return
        blob.data, blob.md5 = copy.pop("data"), copy.pop("md5")
        copy.update(status="success", progress=f"{len(blob.data)}/{len(blob.data)}")
        blob.etag, blob.modified = f'"0x{next(self._etags):X}"', time.time()

    def _list(self, container: str, blobs: dict[str, Blob], query: dict[str, list[str]]) -> Response:
        def field(key: str, default: str | None = None) -> str | None:
            values = query.get(key)
            return values[0] if values else default

        prefix, delimiter, marker = field("prefix", ""), field("delimiter"), field("marker")
        include = {item for value in query.get("include", []) for item in value.split(",")}
        try:
            limit = int(field("maxresults", "5000"))
        except ValueError:
            raise error(400, "InvalidQueryParameterValue", "maxresults isn't a number.")
        if limit <= 0:
            raise error(400, "OutOfRangeQueryParameterValue", "maxresults must be at least 1.")
        limit = min(limit, 5000)
        if self.hns:
            if delimiter not in (None, "/"):
                raise error(400, "InvalidQueryParameterValue", "Only / is a delimiter with a hierarchical namespace.")
            parts = prefix.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                node = blobs.get("/".join(parts[:index]))
                if node is not None and node.kind == "file":  # the docs say "an error will occur"; the code is a guess
                    raise error(409, "PathConflict", "The prefix runs through a file.")
        entries: dict[str, tuple[str, Blob | None]] = {}
        for name in sorted(name for name in blobs if name.startswith(prefix)):
            blob = blobs[name]
            rest = name[len(prefix):]
            if delimiter and delimiter in rest:
                entries.setdefault(prefix + rest[:rest.index(delimiter) + len(delimiter)], ("prefix", None))
            elif self.hns and delimiter and blob.kind == "directory":
                entries[name + "/"] = ("prefix", blob)
            else:
                entries[name] = ("blob", blob)
        ordered = sorted(entries)
        start, skip = "", False
        if marker:
            kind, _, encoded = marker.partition(":")
            try:
                start = base64.b64decode(encoded, validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                raise error(400, "OutOfRangeInput", "The marker isn't one of ours.")
            if kind not in ("after", "skip"):
                raise error(400, "OutOfRangeInput", "The marker isn't one of ours.")
            skip = kind == "skip"
        position = [name for name in ordered if name > start] if start or skip else ordered
        if self.empty_pages and not skip and position:
            page, next_marker = [], "skip:" + base64.b64encode(start.encode()).decode()
        else:
            page = position[:limit]
            next_marker = ("after:" + base64.b64encode(page[-1].encode()).decode()) if len(position) > limit else ""
        parts_xml = [f'<?xml version="1.0" encoding="utf-8"?><EnumerationResults ServiceEndpoint={quoteattr(self.url + "/")} '
                     f'ContainerName={quoteattr(container)}>']
        if "prefix" in query:
            parts_xml.append(f"<Prefix>{escape(prefix)}</Prefix>")
        if marker:
            parts_xml.append(f"<Marker>{escape(marker)}</Marker>")
        if "maxresults" in query:
            parts_xml.append(f"<MaxResults>{limit}</MaxResults>")
        if delimiter:
            parts_xml.append(f"<Delimiter>{escape(delimiter)}</Delimiter>")
        parts_xml.append("<Blobs>")
        for name in page:
            kind, blob = entries[name]
            if kind == "prefix":
                properties = self._properties(blob) if blob is not None else ""
                parts_xml.append(f"<BlobPrefix>{self._name_xml(name)}{properties}</BlobPrefix>")
                continue
            metadata = ("<Metadata>" + "".join(f"<{key}>{escape(value)}</{key}>" for key, value in blob.metadata.items())
                        + "</Metadata>") if "metadata" in include else ""
            parts_xml.append(f"<Blob>{self._name_xml(name)}{self._properties(blob)}{metadata}</Blob>")
        parts_xml.append(f"</Blobs><NextMarker>{escape(next_marker)}</NextMarker></EnumerationResults>")
        return Response(200, "".join(parts_xml).encode(), {"Content-Type": "application/xml"})

    @staticmethod
    def _name_xml(name: str) -> str:
        if "\ufffe" in name or "\uffff" in name:  # not allowed in XML: percent-encoded (version 2021-02-12)
            return f'<Name Encoded="true">{escape(urllib.parse.quote(name, safe="/"))}</Name>'
        return f"<Name>{escape(name)}</Name>"

    def _properties(self, blob: Blob) -> str:
        fields = [("Creation-Time", http_date(blob.created)), ("Last-Modified", http_date(blob.modified)),
                  ("Etag", blob.etag), ("Content-Length", str(len(blob.data))), ("Content-Type", blob.content_type),
                  ("Content-MD5", blob.md5 or ""), ("BlobType", "BlockBlob"), ("LeaseStatus", "unlocked"),
                  ("LeaseState", "available"), ("ServerEncrypted", "true")]
        if self.hns:
            fields.append(("ResourceType", blob.kind))
        return "<Properties>" + "".join(f"<{key}>{escape(value)}</{key}>" for key, value in fields) + "</Properties>"

    # -- writing

    @staticmethod
    def _metadata(req) -> dict[str, str]:
        return {key[len("x-ms-meta-"):]: value for key, value in req.headers.items() if key.lower().startswith("x-ms-meta-")}

    def _make_parents(self, blobs: dict[str, Blob], name: str) -> None:
        """hns: the directories above `name`, made when missing; a file in the way is a conflict."""
        parts = name.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            node = blobs.get(ancestor)
            if node is None:
                blobs[ancestor] = self._directory()
            elif node.kind != "directory":
                raise error(409, "PathConflict", "A file is in the way of this path (code guessed).")

    def _writable(self, blobs: dict[str, Blob], name: str) -> None:
        existing = blobs.get(name)
        if existing is not None and existing.copy is not None and existing.copy["status"] == "pending":
            raise error(409, "PendingCopyOperation", "There is currently a pending copy operation.")
        if self.hns:
            if existing is not None and existing.kind == "directory":
                raise error(409, "PathConflict", "A directory has this name (code guessed).")
            self._make_parents(blobs, name)

    def _put_blob(self, req, container: str, blobs: dict[str, Blob], name: str) -> Response:
        if req.header("Content-Length") is None:
            raise error(411, "MissingContentLengthHeader", "The Content-Length header was not specified.")
        blob_type = req.header("x-ms-blob-type")
        if blob_type is None:
            raise error(400, "MissingRequiredHeader", "x-ms-blob-type is required.")
        if blob_type != "BlockBlob":
            raise error(400, "InvalidHeaderValue", "Only block blobs are faked.")
        metadata = self._metadata(req)
        content_type = req.header("x-ms-blob-content-type") or req.header("Content-Type") or "application/octet-stream"
        if self.hns and name.endswith("/"):  # as rclone saw: "name/" with hdi_isfolder=true makes the directory
            if {key.lower(): value.lower() for key, value in metadata.items()}.get("hdi_isfolder") != "true" or req.body:
                raise error(400, "InvalidUri", "The requested URI does not represent any resource on the server.")
            folder = name[:-1]
            self._make_parents(blobs, folder)
            existing = blobs.get(folder)
            if existing is not None and existing.kind != "directory":
                raise error(409, "PathConflict", "A file has this name (code guessed).")
            blob = existing or blobs.setdefault(folder, self._directory())
            return Response(201, b"", {"ETag": blob.etag, "Last-Modified": http_date(blob.modified)})
        self._writable(blobs, name)
        md5 = base64.b64encode(hashlib.md5(req.body).digest()).decode()
        blob = self._new_blob(req.body, content_type=content_type, metadata=metadata, md5=md5)
        blobs[name] = blob
        self.uncommitted.pop((container, name), None)  # Put Blob drops the uncommitted blocks
        headers = {"ETag": blob.etag, "Last-Modified": http_date(blob.modified), "x-ms-request-server-encrypted": "true",
                   "Content-MD5": md5 if not self.corrupt_md5 else base64.b64encode(b"\0" * 16).decode()}
        return Response(201, b"", headers)

    def _put_block(self, req, container: str, blobs: dict[str, Blob], name: str, query: dict[str, list[str]]) -> Response:
        if req.header("Content-Length") is None:
            raise error(411, "MissingContentLengthHeader", "The Content-Length header was not specified.")
        block_id = (query.get("blockid") or [""])[0]
        try:
            raw = base64.b64decode(block_id, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if not raw or len(raw) > 64:
            raise error(400, "InvalidBlockId", "The specified block ID is invalid. The block ID must be Base64-encoded.")
        existing = blobs.get(name)
        if existing is not None and existing.dfs:
            raise error(409, "BlobOperationNotSupported", "The operation is not supported in this scenario.")
        blocks = self.uncommitted.setdefault((container, name), {})
        if any(len(other) != len(block_id) for other in blocks):
            raise error(400, "InvalidBlobOrBlock", "The specified blob or block content is invalid.")
        blocks[block_id] = req.body
        return Response(201, b"", {"x-ms-request-server-encrypted": "true"})

    def _put_block_list(self, req, container: str, blobs: dict[str, Blob], name: str) -> Response:
        try:
            root = ET.fromstring(req.body)
        except ET.ParseError:
            raise error(400, "InvalidXmlDocument", "The specified XML is not syntactically valid.")
        existing = blobs.get(name)
        if existing is not None and existing.dfs:
            raise error(409, "BlobOperationNotSupported", "The operation is not supported in this scenario.")
        uncommitted = self.uncommitted.get((container, name), {})
        committed = dict(existing.blocks) if existing is not None else {}
        chosen: list[tuple[str, bytes]] = []
        for item in root if root.tag == "BlockList" else []:
            block_id = item.text or ""
            if item.tag == "Latest":
                data = uncommitted.get(block_id, committed.get(block_id))
            elif item.tag == "Uncommitted":
                data = uncommitted.get(block_id)
            elif item.tag == "Committed":
                data = committed.get(block_id)
            else:
                data = None
            if data is None:
                raise error(400, "InvalidBlockList", "The specified block list is invalid.")
            chosen.append((block_id, data))
        if root.tag != "BlockList" or len(chosen) > 50000:
            raise error(400, "InvalidBlockList", "The specified block list is invalid.")
        self._writable(blobs, name)
        content_type = req.header("x-ms-blob-content-type") or "application/octet-stream"
        blob = self._new_blob(b"".join(data for _, data in chosen), content_type=content_type,
                              metadata=self._metadata(req), blocks=chosen)
        blobs[name] = blob
        self.uncommitted.pop((container, name), None)
        return Response(201, b"", {"ETag": blob.etag, "Last-Modified": http_date(blob.modified)})

    def _copy(self, req, blobs: dict[str, Blob], name: str) -> Response:
        source_url = req.header("x-ms-copy-source") or ""
        if len(source_url) > 2048:
            raise error(400, "InvalidHeaderValue", "The copy source is longer than 2 KiB.")
        parts = urllib.parse.urlsplit(source_url)
        if f"{parts.scheme}://{parts.netloc}" != self.url or parts.query:
            raise error(400, "CannotVerifyCopySource", "Only blobs of this account are faked as copy sources.")
        container_raw, _, blob_raw = parts.path[1:].partition("/")
        source_store = self.containers.get(urllib.parse.unquote(container_raw))
        source = source_store["blobs"].get(urllib.parse.unquote(blob_raw)) if source_store is not None else None
        if source is None or source.kind != "file":
            raise error(404, "CannotVerifyCopySource", "The specified blob does not exist.")
        self._writable(blobs, name)
        copy = {"id": str(uuid.uuid4()), "source": source_url, "mode": self.copy_mode}
        if self.copy_mode == "sync":
            copy.update(status="success", progress=f"{len(source.data)}/{len(source.data)}")
            blob = self._new_blob(source.data, content_type=source.content_type, metadata=dict(source.metadata),
                                  md5=source.md5, copy=copy)
        else:  # a committed blob of zero length until the copy ends
            copy.update(status="pending", progress=f"0/{len(source.data)}", polls=self.copy_polls, data=source.data,
                        md5=source.md5)
            blob = self._new_blob(b"", content_type=source.content_type, metadata=dict(source.metadata), copy=copy)
        blobs[name] = blob
        return Response(202, b"", {"ETag": blob.etag, "Last-Modified": http_date(blob.modified),
                                   "x-ms-copy-id": copy["id"], "x-ms-copy-status": copy["status"]})

    def _abort_copy(self, req, blobs: dict[str, Blob], name: str, query: dict[str, list[str]]) -> Response:
        if req.header("x-ms-copy-action") != "abort":
            raise error(400, "MissingRequiredHeader", "x-ms-copy-action: abort is required.")
        blob = blobs.get(name)
        if blob is None:
            raise error(404, "BlobNotFound", "The specified blob does not exist.")
        if blob.copy is None or blob.copy["status"] != "pending":
            raise error(409, "NoPendingCopyOperation", "There is currently no pending copy operation.")
        if (query.get("copyid") or [""])[0] != blob.copy["id"]:
            raise error(409, "CopyIdMismatch", "The specified copy ID did not match the copy ID for the pending copy operation.")
        blob.copy.update(status="aborted")
        blob.copy.pop("data", None)
        return Response(204, b"")

    def _delete(self, req, container: str, blobs: dict[str, Blob], name: str) -> Response:
        blob = blobs.get(name)
        if blob is None:
            if self.uncommitted.pop((container, name), None) is not None:  # an uncommitted blob (2013-08-15)
                return Response(202, b"")
            raise error(404, "BlobNotFound", "The specified blob does not exist.")
        if blob.kind == "directory" and any(other.startswith(name + "/") for other in blobs):
            raise error(409, "DirectoryIsNotEmpty", "The directory isn't empty (code guessed).")
        if blob.snapshots and req.header("x-ms-delete-snapshots") != "include":
            raise error(409, "SnapshotsPresent", "This operation is not permitted while the blob has snapshots.")
        del blobs[name]
        self.uncommitted.pop((container, name), None)
        return Response(202, b"", {"x-ms-delete-type-permanent": "true"})
