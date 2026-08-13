"""Dialling a deployment's object storage (task E5.4e; spec 16.2 row 5).

**boto3, through `asyncio.to_thread`.** boto3 rather than a hand-rolled SigV4
signer because signing against MinIO, Ceph and real S3 differs in exactly the
details a hand-rolled signer gets wrong - path versus virtual-host addressing,
region resolution, chunked payload signing - and getting one of them wrong
produces a `SignatureDoesNotMatch` that looks identical to a wrong secret key.
It is synchronous, so every call goes to a thread, following
`controlplane/broker.py`'s precedent for synchronous work on the event loop.

**`forbidden` and `not_found` are different answers and the distinction is the
unit's acceptance criterion.** A bucket that does not exist and a bucket that
exists and refuses this key send the operator to two different places - create
the bucket, versus fix the policy on it. S3 makes this harder than it sounds:
`HeadBucket` answers **404 for both** when the credentials lack
`s3:ListBucket`, because AWS deliberately does not confirm the existence of
buckets you cannot see. So the classification reads the error CODE where one
is given and falls back to the HTTP status, and the tester's remedy names both
possibilities where the two genuinely cannot be told apart.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.clients.httpbase import ServiceFailure, safe_endpoint

#: The prefix every object this tester writes lives under. Reserved, constant
#: and platform-named so an operator seeing it in a listing knows whose it is,
#: and so the cleanup assertion can list exactly it.
SELFTEST_PREFIX = "_eoe_selftest/"

#: The single zero-byte object written and then deleted.
SELFTEST_KEY = f"{SELFTEST_PREFIX}connection-test"


@dataclass(frozen=True)
class S3Client:
    """One deployment's object store, dialable.

    `secret_key` is `field(repr=False)` behind a `__str__` naming only the
    endpoint and the bucket - the D66 precedent, and the reason it matters
    here is that this credential can read every raw recording the deployment
    has ever uploaded.
    """

    bucket: str
    endpoint: str | None = None
    region: str | None = None
    access_key: str = ""
    secret_key: str = field(repr=False, default="")
    timeout: float = 10.0

    def __str__(self) -> str:
        where = safe_endpoint(self.endpoint) if self.endpoint else "aws s3"
        return f"s3 {where} bucket {self.bucket}"

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(one for one in (self.secret_key,) if one)

    def _client(self) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint or None,
            aws_access_key_id=self.access_key or None,
            aws_secret_access_key=self.secret_key or None,
            # Real AWS requires a region; MinIO ignores it. us-east-1 is the
            # value AWS itself treats as "no specific region".
            region_name=self.region or "us-east-1",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 1},
                connect_timeout=self.timeout,
                read_timeout=self.timeout,
            ),
        )

    def classify(self, error: BaseException) -> ServiceFailure:
        """One botocore error, in terms an operator can act on."""
        from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

        where = str(self)
        if isinstance(error, NoCredentialsError):
            return ServiceFailure(
                kind="auth",
                detail=f"{where} was dialled without an access key or secret key",
                remedy="enter the object storage access key and secret key, then test again",
            )
        if isinstance(error, EndpointConnectionError):
            return ServiceFailure(
                kind="unreachable",
                detail=f"nothing answered at {where}",
                remedy=(
                    "check the endpoint URL and that the object store is reachable from the "
                    "platform host; leave the endpoint blank for real AWS S3"
                ),
            )
        if isinstance(error, ClientError):
            return self._classify_client_error(error, where)
        return ServiceFailure(
            kind="unreachable",
            detail=f"the request to {where} failed ({type(error).__name__})",
            remedy=(
                "check the endpoint, region and bucket name, and that the object store is "
                "reachable from the platform host"
            ),
        )

    def _classify_client_error(self, error: Any, where: str) -> ServiceFailure:
        response: Mapping[str, Any] = getattr(error, "response", {}) or {}
        info: Mapping[str, Any] = response.get("Error", {}) or {}
        code = str(info.get("Code", ""))
        status = int((response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode", 0) or 0)

        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidClientTokenId") or (
            status == 401
        ):
            return ServiceFailure(
                kind="auth",
                detail=f"{where} rejected the access key or secret key ({code or status})",
                remedy=(
                    "check the access key and secret key. A secret key that has been rotated in "
                    "the object store but not here fails exactly like this"
                ),
            )
        if code in ("AccessDenied", "AllAccessDisabled") or status == 403:
            return ServiceFailure(
                kind="forbidden",
                detail=(
                    f"{where} exists and these credentials are not allowed to use it "
                    f"({code or status})"
                ),
                remedy=(
                    "grant this key access to the bucket. The platform needs to head the "
                    "bucket and to put, list and delete objects under it; the credentials "
                    "themselves were accepted, so this is a bucket policy rather than a "
                    "wrong key"
                ),
            )
        if code in ("NoSuchBucket", "404") or status == 404:
            return ServiceFailure(
                kind="not_found",
                detail=f"{where} does not exist, or this key may not see it ({code or status})",
                remedy=(
                    "create the bucket, or correct the bucket name. If it does exist, grant "
                    "this key s3:ListBucket on it - S3 answers 404 rather than 403 for a "
                    "bucket the caller is not allowed to know about, so these two cannot be "
                    "told apart from outside"
                ),
            )
        return ServiceFailure(
            kind="unexpected_status",
            detail=f"{where} answered {code or status or type(error).__name__}",
            remedy=(
                "check the endpoint, region and bucket; the code above is what the object "
                "store returned"
            ),
        )

    async def head_bucket(self) -> None:
        client = await asyncio.to_thread(self._client)
        await asyncio.to_thread(client.head_bucket, Bucket=self.bucket)

    async def put_probe_object(self) -> None:
        client = await asyncio.to_thread(self._client)
        await asyncio.to_thread(client.put_object, Bucket=self.bucket, Key=SELFTEST_KEY, Body=b"")

    async def delete_probe_object(self) -> None:
        client = await asyncio.to_thread(self._client)
        await asyncio.to_thread(client.delete_object, Bucket=self.bucket, Key=SELFTEST_KEY)

    async def list_probe_prefix(self) -> list[str]:
        """Every key under the reserved prefix. The cleanup assertion reads
        this, so "the prefix is empty after a pass" is checked against the
        store rather than inferred from the delete returning success."""
        client = await asyncio.to_thread(self._client)
        page = await asyncio.to_thread(
            client.list_objects_v2, Bucket=self.bucket, Prefix=SELFTEST_PREFIX
        )
        return [str(row["Key"]) for row in page.get("Contents", []) if "Key" in row]
