"""A local S3 for driver tests: moto's ThreadedMotoServer on 127.0.0.1, a random port.

moto serves path-style requests at http://127.0.0.1:<port>/<bucket>/<key>. Virtual-hosted
requests ("<bucket>.s3.test:<port>") reach the same server through ToLocal, a transport that
connects to 127.0.0.1 and keeps the Host header, from which moto takes the bucket.

moto ignores signatures by default. Inside `checking_signatures()` it checks the Authorization
header of every request like AWS does (botocore recomputes the signature from the request it
received), for an IAM user of its own; presigned URLs it can't check.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import Iterator

import httpx

DUMMY_AUTH = {
    # moto reads the region from this header when it doesn't check signatures.
    "Authorization": "AWS4-HMAC-SHA256 Credential=AKIDTEST/20260101/us-east-1/s3/aws4_request, "
                     "SignedHeaders=host, Signature=0",
    "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
    "x-amz-date": "20260101T000000Z",
}


class MotoS3:
    def __init__(self):
        from moto.server import ThreadedMotoServer

        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self.server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        self.server.start()
        self.port = self.server.get_host_and_port()[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self._user: tuple[str, str] | None = None

    def stop(self) -> None:
        self.server.stop()

    def bucket(self, prefix: str = "t", suffix: str = "") -> str:
        """A new, empty bucket."""
        name = f"{prefix}{uuid.uuid4().hex[:16]}{suffix}"
        response = httpx.put(f"{self.endpoint}/{name}", headers=DUMMY_AUTH, timeout=30)
        response.raise_for_status()
        return name

    def keys(self, bucket: str, prefix: str = "") -> list[str]:
        """Every key in the bucket (or below prefix), straight from moto's memory."""
        for backend in self._backends():
            if bucket in backend.buckets:
                return sorted(key for key in backend.buckets[bucket].keys.keys() if key.startswith(prefix))
        raise KeyError(bucket)

    def put_raw(self, bucket: str, key: str, data: bytes = b"x") -> None:
        """Store an object as another program would, with any key."""
        for backend in self._backends():
            if bucket in backend.buckets:
                backend.put_object(bucket, key, data)
                return
        raise KeyError(bucket)

    def uploads(self, bucket: str) -> list[str]:
        """Multipart uploads in progress."""
        for backend in self._backends():
            if bucket in backend.buckets:
                return list(backend.buckets[bucket].multiparts)
        raise KeyError(bucket)

    @staticmethod
    def _backends():
        from moto.s3.models import s3_backends

        for account in s3_backends.values():
            yield from account.values()

    def transport(self) -> httpx.HTTPTransport:
        return ToLocal(self.port)

    @contextlib.contextmanager
    def checking_signatures(self) -> Iterator[tuple[str, str]]:
        """moto checks the signature of every request while inside; yields an IAM user's
        (access key, secret key) that may do anything."""
        from moto import settings
        from moto.core import DEFAULT_ACCOUNT_ID
        from moto.core.authorization import ActionAuthenticatorMixin
        from moto.iam.models import iam_backends

        if self._user is None:
            iam = iam_backends[DEFAULT_ACCOUNT_ID]["global"]
            name = f"tester{uuid.uuid4().hex[:8]}"
            iam.create_user("us-east-1", name)
            iam.put_user_policy(name, "everything", json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}))
            key = iam.create_access_key(name)
            self._user = (key.access_key_id, key.secret_access_key)
        old = settings.INITIAL_NO_AUTH_ACTION_COUNT, ActionAuthenticatorMixin.request_count
        settings.INITIAL_NO_AUTH_ACTION_COUNT = 0
        ActionAuthenticatorMixin.request_count = 0
        try:
            yield self._user
        finally:
            settings.INITIAL_NO_AUTH_ACTION_COUNT, ActionAuthenticatorMixin.request_count = old


class ToLocal(httpx.HTTPTransport):
    """Sends every request to 127.0.0.1:<port> whatever the host of its URL; the Host header
    stays as it was, so moto sees "<bucket>.s3.test:<port>"."""

    def __init__(self, port: int):
        super().__init__()
        self.port = port

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return super().handle_request(request)
