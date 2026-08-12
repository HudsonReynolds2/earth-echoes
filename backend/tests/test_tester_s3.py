"""E5.4e: the object storage tester, against a real MinIO.

MinIO rather than a fake because SigV4 is the thing most likely to be wrong,
and a fake would be signed by the same code that signs the real request - it
would agree with itself no matter what either of them did.

Three properties this file is answerable to (phase document, E5.4e): the
reserved prefix is empty after a pass, asserted by listing it; a bucket that
exists but denies the key fails `forbidden` rather than `not_found`; and object
storage reports `not_required` rather than `fail` when the deployment is not
using it, because spec 16.2 makes it conditionally required and a red dot for
an unused optional service trains operators to ignore red.
"""

import asyncio
import json
import uuid

import pytest
from conftest import (
    RIG_PASSWORD,
    RIG_USER,
)

from app.services.clients.s3 import SELFTEST_PREFIX, S3Client
from app.services.testers.base import ServiceCredentials
from app.services.testers.s3 import S3Tester

# `rig` is NOT imported: it is a fixture defined in conftest.py and pytest
# discovers it automatically. Importing it binds a SEPARATE fixture object
# into this module, and "session" scope then applies per copy - which built
# the five-container rig once per module and silently undid the phase-5
# section 5 gate-time design (measured: 15 containers, not 5).
pytestmark = pytest.mark.integration


def credentials(
    endpoint: str | None,
    bucket: str,
    access_key: str = RIG_USER,
    secret_key: str = RIG_PASSWORD,
) -> ServiceCredentials:
    secrets = {}
    if access_key:
        secrets["access_key"] = access_key
    if secret_key:
        secrets["secret_key"] = secret_key
    return ServiceCredentials(
        service_key="s3",
        settings={"bucket": bucket, "endpoint": endpoint, "region": "us-east-1"},
        secrets=secrets,
    )


def checks_by_name(result):
    return {check.name: check for check in result.checks}


def run(creds: ServiceCredentials):
    return asyncio.run(S3Tester().run(creds))


def client_for_rig(rig, bucket: str | None = None) -> S3Client:
    return S3Client(
        bucket=bucket or rig.bucket,
        endpoint=rig.minio.url,
        region="us-east-1",
        access_key=RIG_USER,
        secret_key=RIG_PASSWORD,
    )


def test_a_correctly_configured_bucket_passes_every_check(rig):
    result = run(credentials(rig.minio.url, rig.bucket))
    assert result.outcome == "pass", result.checks
    assert set(checks_by_name(result)) == {"head_bucket", "write", "cleanup"}


def test_the_reserved_prefix_is_empty_afterwards(rig):
    """The acceptance criterion, asserted by listing the prefix from outside
    the tester rather than trusting its own cleanup check."""
    run(credentials(rig.minio.url, rig.bucket))
    remaining = asyncio.run(client_for_rig(rig).list_probe_prefix())
    assert remaining == [], remaining


def test_the_tester_is_repeatable(rig):
    for _ in range(2):
        assert run(credentials(rig.minio.url, rig.bucket)).outcome == "pass"
    assert asyncio.run(client_for_rig(rig).list_probe_prefix()) == []


def test_a_missing_bucket_fails_not_found(rig):
    result = run(credentials(rig.minio.url, f"no-such-bucket-{uuid.uuid4().hex[:8]}"))
    assert result.outcome == "fail"
    head = checks_by_name(result)["head_bucket"]
    assert not head.passed
    assert "does not exist" in head.detail
    assert head.remedy


def test_a_bucket_that_denies_the_key_fails_forbidden_and_not_not_found(rig):
    """The distinction the acceptance criterion names.

    A restricted MinIO user is created with a policy that grants nothing on
    the rig bucket. The bucket demonstrably EXISTS - the admin key heads it in
    the same test - so an answer of `not_found` here would send the operator
    to create a bucket they already have.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    name = f"denied{uuid.uuid4().hex[:8]}"
    password = "denied-user-password"
    policy = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Deny", "Action": "s3:*", "Resource": ["arn:aws:s3:::*"]}],
    }
    import subprocess

    from conftest import docker_cli, docker_env

    def mc(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                docker_cli(),
                "run",
                "--rm",
                "--network",
                "host",
                "--entrypoint",
                "sh",
                "minio/mc:latest",
                "-c",
                " && ".join(
                    [
                        f"mc alias set rig {rig.minio.url} {RIG_USER} {RIG_PASSWORD}",
                        *args,
                    ]
                ),
            ],
            capture_output=True,
            text=True,
            env=docker_env(),
            timeout=120,
        )

    written = mc(
        f"echo '{json.dumps(policy)}' > /tmp/deny.json",
        "mc admin policy create rig denyall /tmp/deny.json",
        f"mc admin user add rig {name} {password}",
        f"mc admin policy attach rig denyall --user {name}",
    )
    assert written.returncode == 0, (
        f"could not create the denied user:\n{written.stdout}\n{written.stderr}"
    )

    # The bucket exists: the admin key can head it.
    asyncio.run(client_for_rig(rig).head_bucket())

    result = run(credentials(rig.minio.url, rig.bucket, access_key=name, secret_key=password))
    assert result.outcome == "fail"
    head = checks_by_name(result)["head_bucket"]
    assert not head.passed, head

    # MinIO answers 403 for a denied HeadBucket, so this must read as
    # `forbidden`. The remedy talks about policy, not about creating a bucket.
    assert "not allowed" in head.detail or "exists" in head.detail, head.detail
    assert "policy" in head.remedy.lower() or "grant" in head.remedy.lower()

    _ = ClientError, boto3, Config  # imported for the reader; MinIO does the work


def test_wrong_credentials_fail_auth(rig):
    result = run(
        credentials(rig.minio.url, rig.bucket, access_key="nobody", secret_key="wrongsecret")
    )
    assert result.outcome == "fail"
    head = checks_by_name(result)["head_bucket"]
    assert not head.passed
    assert head.remedy


def test_no_credentials_is_not_required_rather_than_a_failure(rig):
    """Spec 16.2 makes object storage CONDITIONALLY required.

    A deployment not uploading raw audio through the platform has no keys for
    it, and reporting that red would train operators to ignore red. The
    outcome is `not_required`, which E5.5 records on the row so the rollup
    agrees and the deployment can still reach `verified`.
    """
    result = run(credentials(rig.minio.url, rig.bucket, access_key="", secret_key=""))
    assert result.outcome == "not_required"
    assert all(check.passed for check in result.checks)
    assert result.checks[0].remedy == ""


def test_a_half_entered_form_is_still_tested_for_real(rig):
    """`not_required` needs BOTH credentials absent. One present means the
    operator is configuring it and got it wrong, which must fail loudly rather
    than be excused as "not in use"."""
    result = run(credentials(rig.minio.url, rig.bucket, access_key=RIG_USER, secret_key=""))
    assert result.outcome == "fail"
    assert checks_by_name(result)["head_bucket"].remedy


def test_an_unreachable_endpoint_fails_with_a_remedy(rig):
    from conftest import free_port

    result = run(credentials(f"http://127.0.0.1:{free_port()}", rig.bucket))
    assert result.outcome == "fail"
    assert result.checks[0].remedy


def test_no_secret_key_appears_in_any_result_or_log(rig, caplog):
    with caplog.at_level("DEBUG"):
        results = [
            run(credentials(rig.minio.url, rig.bucket)),
            run(credentials(rig.minio.url, "nope")),
        ]
    blob = repr(results) + "\n".join(record.getMessage() for record in caplog.records)
    assert RIG_PASSWORD not in blob


def test_the_reserved_prefix_is_named_and_platform_owned():
    """An operator seeing this in a listing must be able to tell whose it is,
    and the cleanup assertion needs a prefix nothing else writes under."""
    assert SELFTEST_PREFIX.startswith("_eoe")
    assert SELFTEST_PREFIX.endswith("/")
