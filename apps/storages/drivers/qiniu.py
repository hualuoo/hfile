"""Qiniu Kodo through its S3-compatible API (see s3.py for paths, folders and limits).

Paths
    Paths map onto keys of the bucket exactly as in s3.py: the base path is a key prefix, folders are prefixes with
    optional "name/" markers.

Params
    The S3 endpoint of the bucket's region (https://s3.cn-east-1.qiniucs.com; the region id for signing comes from it),
    the access and secret keys, the bucket's S3 name (equal to the bucket name when that is unique), the base path,
    public or private, and the download domain.

Downloads
    Qiniu's S3 endpoints don't serve anonymous requests, so links go through the domain bound to the bucket (CDN or
    origin domain): a private bucket gets Qiniu's own download token (`?e=<deadline>&token=<AccessKey>:<signature>`), a
    public one the plain URL, and both `attname=<file name>` so that the browser saves the file under its name. Without
    a domain the link is a presigned S3 URL of the endpoint, for public buckets too.

Limits
    Those of s3.py; Qiniu's ListObjectsV2 doesn't support encoding-type=url, so keys with characters that XML can't
    carry can't be listed. Buckets created since 2026-04-08 don't serve .apk, .ipa, .exe and similar files through the
    S3 domains, so reads of them through the site fail; they still go through S3, not the domain, which is often a CDN
    whose cache may hold an older version of a file (the editors save what they read).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from typing import ClassVar

from django.utils.translation import gettext_lazy

from .base import Param
from .s3 import (
    ACCESS_KEY,
    BASE_PATH,
    IS_PRIVATE,
    MAX_PRESIGN_SECONDS,
    SECRET_KEY,
    S3Driver,
    bucket_param,
    content_disposition,
    domain_param,
    parse_origin,
    uri_encode,
)

# The S3 service domains of Kodo: s3.<region id>.qiniucs.com.
_ENDPOINT = re.compile(r"s3[.-]([a-z0-9-]+)\.qiniucs\.com(?::\d+)?")


def private_url(url: str, deadline: int, access_key: str, secret_key: str) -> str:
    """Qiniu's download token: sign "url?...&e=deadline" with HMAC-SHA1 and append the token
    (it has to come last)."""
    url += ("&" if "?" in url else "?") + f"e={deadline}"
    digest = hmac.new(secret_key.encode(), url.encode(), hashlib.sha1).digest()
    return url + f"&token={access_key}:{base64.urlsafe_b64encode(digest).decode()}"


class QiniuDriver(S3Driver):
    type = "qiniu"
    label = gettext_lazy("Qiniu Kodo")
    params = (
        Param("endpoint", gettext_lazy("S3 endpoint"),
              help=gettext_lazy("The S3 endpoint of the bucket's region, with the scheme, like "
                   "https://s3.cn-east-1.qiniucs.com (East China, Zhejiang), "
                   "https://s3.cn-north-1.qiniucs.com (North China, Hebei) or "
                   "https://s3.cn-south-1.qiniucs.com (South China, Guangdong)."),
              placeholder="https://s3.cn-east-1.qiniucs.com"),
        ACCESS_KEY,
        SECRET_KEY,
        bucket_param(gettext_lazy("The bucket's S3 name, shown in the Qiniu console's \"Bucket overview\"; "
                                  "when the bucket name is globally unique, it's the bucket name."), gettext_lazy("S3 bucket name")),
        BASE_PATH,
        IS_PRIVATE,
        domain_param(gettext_lazy("The CDN or origin domain bound to this bucket, written as https://domain. "
                     "Qiniu's S3 endpoint doesn't allow anonymous access, so download links use "
                     "this domain: with Qiniu's download token for private buckets, plain links "
                     "for public ones. When empty, S3-signed links are used. Buckets created since "
                     "April 8, 2026 don't allow downloading .apk, .ipa, .exe and similar files through "
                     "the S3 domain: such files need this domain, and downloads through the site fail "
                     "for them.")),
    )
    force_path_style = False
    url_encoded_listing = False  # Kodo's ListObjectsV2 ignores encoding-type
    endpoint_example: ClassVar[str] = "https://s3.cn-east-1.qiniucs.com"

    def _region(self) -> str:
        host = parse_origin(self.config.get("endpoint") or "")[1]
        match = _ENDPOINT.fullmatch(host)
        return match.group(1) if match else "kodo"

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        domain = (self.config.get("domain") or "").strip()
        if not domain:  # the S3 endpoint takes signed requests only, public bucket or not
            query = [("response-content-disposition", content_disposition(filename))] if filename else []
            return self._s3().presign("GET", self._key(path), expires=expires, query=query)
        scheme, host = parse_origin(domain)
        url = f"{scheme}://{host}/{uri_encode(self._key(path), keep_slash=True)}"
        if filename:
            url += f"?attname={uri_encode(filename)}"
        if not self.config.get("is_private", True):
            return url
        deadline = int(time.time()) + max(1, min(int(expires), MAX_PRESIGN_SECONDS))
        return private_url(url, deadline, str(self.config.get("access_key") or ""),
                           str(self.config.get("secret_key") or ""))
