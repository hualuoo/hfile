"""DogeCloud (多吉云) object storage: temporary S3 credentials from DogeCloud's API, then S3. DogeCloud's storage is
Tencent COS underneath.

Paths
    Paths, folders and limits are those of s3.py (virtual-hosted S3 at the returned endpoint).

Login
    POST /auth/tmp_token.json (signed with the account's AccessKey and SecretKey: Authorization "TOKEN <AccessKey>:<hex
    HMAC-SHA1 of path + "\\n" + body>") gives credentials with a session token (valid for up to two hours), the bucket's
    S3 endpoint and its S3 bucket name. They are cached in memory and fetched again shortly before they expire, under a
    lock so that one thread fetches while the others wait. They are not saved: a new pair is a request away. DogeCloud
    may move a bucket (its endpoint and S3 name "有可能发生变化"): the S3 client is made again for the new ones, and
    requests still reading from the old client finish there.

Params
    AccessKey and SecretKey (DogeCloud's, from 用户中心 → 密钥管理), the bucket's name as the DogeCloud console shows
    it, the base path, and an optional download domain (the bucket's CDN domain).

Downloads
    With a domain, download links are plain URLs of the domain; without one, they are presigned S3 URLs, valid no longer
    than the temporary credentials (about two hours).

Limits
    Only standard (标准型) buckets can be used: basic (基础型) ones have no API or SDK access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import httpx

from django.utils.translation import gettext as _, gettext_lazy, pgettext_lazy

from . import http
from .base import AuthError, BackendError, DriverError, Param, Unsupported, UploadTarget
from .s3 import (
    BASE_PATH,
    Credentials,
    S3Client,
    S3Driver,
    check_base_path_and_domain,
    check_key,
    domain_param,
    parse_origin,
    uri_encode,
)


def api_signature(secret_key: str, path: str, body: str) -> str:
    """DogeCloud's API signature: hex HMAC-SHA1 of the path with its query, "\\n" and the body."""
    return hmac.new(secret_key.encode(), f"{path}\n{body}".encode(), hashlib.sha1).hexdigest()


class DogeCloudDriver(S3Driver):
    type = "doge-cloud"
    label = gettext_lazy("DogeCloud")
    params = (
        Param("access_key", "AccessKey", help=gettext_lazy("The AccessKey from DogeCloud's \"User center → Key management\".")),
        Param("secret_key", "SecretKey", kind="password", secret=True, help=gettext_lazy("The SecretKey that goes with the AccessKey.")),
        Param("bucket", pgettext_lazy("Aliyun, DogeCloud", "Bucket name"),
              help=gettext_lazy("The bucket name shown in the DogeCloud console. It must be a standard bucket: in "
                                "basic buckets, files can't be uploaded or managed through the API. Direct uploads "
                                "from the browser use addresses signed with temporary keys, so DogeCloud buckets need "
                                "no CORS settings.")),
        BASE_PATH,
        domain_param(gettext_lazy("Optional. The CDN domain bound to the bucket, written as https://domain. "
                     "When set, download links are this domain plus the file path (unsigned; "
                     "set hotlink protection in the DogeCloud console); otherwise links are "
                     "signed with temporary keys and last about two hours at most. The storage "
                     "underneath is Tencent Cloud COS, whose buckets created since 2024 don't allow "
                     "downloading .apk and .ipa files through their default domain: newer buckets may "
                     "need this domain for such files, and downloads through the site may fail for them.")),
    )
    helpers = {"buckets": pgettext_lazy("Aliyun, DogeCloud", "List buckets")}
    signing_region = "automatic"
    force_path_style = False
    list_version = 1  # COS underneath

    # Endpoint and tuning; tests point them at a fake.
    api_url: ClassVar[str] = "https://api.dogecloud.com"
    token_seconds: ClassVar[int] = 7200  # the longest DogeCloud gives
    refresh_margin: ClassVar[int] = 300  # fetch new credentials when they expire within this

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._token_lock = threading.Lock()
        self._credentials_now: Credentials | None = None
        self._lifetime: float = self.token_seconds  # how long fetched credentials last
        self._bucket_target: tuple[str, str] | None = None  # (S3 endpoint, S3 bucket), as last fetched
        self._client_target: tuple[str, str] | None = None  # the one the S3 client was made for
        self._retired: list[S3Client] = []  # clients of a bucket's old target, closed with the driver
        self._api_http = http.client()

    def close(self) -> None:
        super().close()
        with self._client_lock:
            for client in self._retired:
                client.close()
            self._retired.clear()
        self._api_http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if (message := cls._check_bucket(config.get("bucket"))):
            errors["bucket"] = message.replace("存储桶", "存储空间")
        if (message := check_key(config.get("access_key"), "AccessKey")):
            errors["access_key"] = message
        if not str(config.get("secret_key") or "").strip():
            errors["secret_key"] = _("Please fill in the SecretKey.")
        errors.update(check_base_path_and_domain(config))
        return errors

    @classmethod
    def helper(cls, name: str, config: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        if name != "buckets":
            raise Unsupported()
        if (message := check_key(config.get("access_key"), "AccessKey")) or not config.get("secret_key"):
            raise DriverError(message or _("Please fill in the SecretKey first."))
        driver = cls(config)
        try:
            data = driver._api("/oss/bucket/list.json")
        finally:
            driver.close()
        try:
            names = [str(item["name"]) for item in data["buckets"]]
        except (KeyError, TypeError) as exc:
            raise BackendError(_("The DogeCloud bucket list answer is missing fields.")) from exc
        if not names:
            return {"message": _("This account has no buckets yet.")}
        return {"param": "bucket", "choices": [{"value": name, "label": name} for name in names]}

    # -- login

    def _api(self, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        """Call DogeCloud's API (POST with a JSON body, or GET); the "data" of a successful answer."""
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else ""
        signature = api_signature(str(self.config.get("secret_key") or ""), path, body)
        headers = {"Authorization": f"TOKEN {self.config.get('access_key') or ''}:{signature}"}
        try:
            if payload is None:
                response = self._api_http.get(self.api_url + path, headers=headers)
            else:
                headers["Content-Type"] = "application/json"
                response = self._api_http.post(self.api_url + path, content=body.encode(), headers=headers)
        except httpx.HTTPError as exc:
            raise BackendError(_("Connecting to the DogeCloud API failed (%(error)s).") % {"error": type(exc).__name__}) from exc
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise BackendError(_("The DogeCloud API's answer couldn't be understood (HTTP %(status_code)s).") % {"status_code": response.status_code})
        code, message = answer.get("code"), str(answer.get("msg") or "")[:200]
        if code == 200 and response.status_code == 200:
            return answer.get("data")
        self.log.info("DogeCloud API %s: HTTP %s, code %s, %s", path, response.status_code, code, message)
        if response.status_code in (401, 403) or code in (401, 403) or "UNAUTHORIZED" in message.upper():
            raise AuthError(_("DogeCloud refused the request (%(code)s %(message)s): "
                              "please check the AccessKey and SecretKey.") % {"code": code, "message": message})
        raise BackendError(_("The DogeCloud API failed (%(code)s %(message)s).") % {"code": code, "message": message})

    def _fetch_credentials(self) -> Credentials:
        bucket = str(self.config.get("bucket") or "")
        data = self._api("/auth/tmp_token.json",
                         {"channel": "OSS_FULL", "ttl": self.token_seconds, "scopes": [bucket]})
        try:
            given = data["Credentials"]
            credentials = Credentials(str(given["accessKeyId"]), str(given["secretAccessKey"]),
                                      str(given["sessionToken"]), float(data["ExpiredAt"]))
            buckets = data.get("Buckets") or []
            info = next((item for item in buckets if item.get("name") == bucket), buckets[0] if buckets else None)
            if info is None:
                raise BackendError(_("DogeCloud has no bucket named %(bucket)s. Please check the bucket name.") % {"bucket": bucket})
            target = (str(info["s3Endpoint"]), str(info["s3Bucket"]))
            parse_origin(target[0])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise BackendError(_("The DogeCloud temporary key API's answer is missing fields.")) from exc
        if self._bucket_target not in (None, target):  # the credentials are for the new one: _s3() follows
            self.log.warning("DogeCloud moved bucket %s to %s", bucket, target)
        self._bucket_target = target
        self.log.debug("DogeCloud credentials for %s valid until %s", bucket, credentials.expires_at)
        return credentials

    def _credentials(self, valid_for: float = 0) -> Credentials:
        """The temporary credentials, fetched again when they expire within refresh_margin
        seconds, or within `valid_for` as far as new ones would last longer."""
        with self._token_lock:
            need = max(self.refresh_margin, min(valid_for, self._lifetime - self.refresh_margin))
            # DogeCloud's ExpiredAt is its own time: use the clock offset the S3 client learnt.
            now = time.time() + (self._client.clock_offset if self._client is not None else 0.0)
            credentials = self._credentials_now
            if credentials is None or (credentials.expires_at or 0) - now < need:
                credentials = self._credentials_now = self._fetch_credentials()
                self._lifetime = (credentials.expires_at or now) - now
            return credentials

    def _forget_credentials(self) -> None:
        """S3 refused them (expired or revoked): the next request fetches new ones."""
        with self._token_lock:
            self._credentials_now = None

    # -- requests

    def _make_client(self) -> S3Client:
        self._credentials()
        endpoint, bucket = self._client_target = self._bucket_target  # type: ignore[misc]
        return S3Client(endpoint=endpoint, bucket=bucket, region=self.signing_region or "automatic",
                        credentials=self._credentials, renew_credentials=self._forget_credentials,
                        path_style=False, list_version=self.list_version, page_size=self.page_size,
                        transport=self.transport, logger=self.log)

    def _s3(self) -> S3Client:
        """The client of the bucket's current target. Credentials due for renewal are fetched here, before a
        request, so that the request goes where the new ones belong."""
        self._credentials()
        if self._client is not None and self._client_target != self._bucket_target:
            with self._client_lock:
                if self._client is not None and self._client_target != self._bucket_target:
                    self._retired.append(self._client)  # streams still being read from it go on
                    self._client = None
        return super()._s3()

    # -- files

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        domain = (self.config.get("domain") or "").strip()
        if domain:
            scheme, host = parse_origin(domain)
            return f"{scheme}://{host}/{uri_encode(self._key(path), keep_slash=True)}"
        credentials = self._credentials(valid_for=expires)
        now = time.time() + (self._client.clock_offset if self._client is not None else 0.0)
        left = int((credentials.expires_at or 0) - now) - 10
        return super().download_url(path, filename=filename, expires=max(1, min(expires, left)))

    def upload_target(self, path: str, size: int) -> UploadTarget | None:
        self._credentials(valid_for=self.upload_url_seconds)
        return super().upload_target(path, size)
