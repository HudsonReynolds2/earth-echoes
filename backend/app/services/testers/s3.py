"""The object storage connection tester (task E5.4e; spec 16.2 row 5).

Three checks - head the bucket, put a zero-byte object under a reserved prefix,
delete it and prove the prefix is empty - and one outcome that is not a check.

## Why this tester can answer `not_required`, and when

Spec 16.2 makes object storage **conditionally** required, and the phase
document is explicit that reporting it red would "train operators to ignore
red". So this is the one tester that can answer `not_required` (E5.3's D111
vocabulary), and E5.5 persists that answer onto `deployment_service.required`
so the deployment can still reach `verified` (D117).

**What "the condition" is, concretely.** The settings catalog has no
`upload.raw_audio_enabled` key - it carries `upload.s3_bucket`, `s3_prefix`,
`s3_endpoint`, `s3_access_key` and `s3_secret_key` and nothing that switches
the feature as such. So the observable condition is **whether the platform has
credentials to upload with**: a service entry carrying no access key and no
secret key describes a deployment whose Aggregators are not shipping raw audio
anywhere the platform is party to, and dialling it would fail on an
authentication error that means nothing.

That is a reading of the spec rather than a quotation of it, and it is drawn
here rather than buried: an S3 entry with **either** credential present is
treated as in use and is tested for real, so a half-entered form fails loudly
instead of being excused. A deployment that is genuinely not using object
storage has no S3 entry at all and never reaches this tester - E5.3's runner
answers `not_configured` first.
"""

import time
from dataclasses import dataclass

from app.services.clients.s3 import SELFTEST_KEY, SELFTEST_PREFIX, S3Client
from app.services.testers.base import (
    CheckResult,
    ServiceCredentials,
    TestResult,
)

#: Three round trips plus a list, each on a thread. Object stores across a WAN
#: are the slowest of the five services here.
S3_BUDGET_SECONDS = 20.0


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def client_for(credentials: ServiceCredentials) -> S3Client:
    settings = credentials.settings
    return S3Client(
        bucket=str(settings["bucket"]),
        endpoint=(str(settings["endpoint"]) if settings.get("endpoint") else None),
        region=(str(settings["region"]) if settings.get("region") else None),
        access_key=credentials.secrets.get("access_key", ""),
        secret_key=credentials.secrets.get("secret_key", ""),
    )


@dataclass
class S3Tester:
    """Spec 16.2's object storage test. Registered as `REGISTRY["s3"]`."""

    service_key: str = "s3"
    budget_seconds: float = S3_BUDGET_SECONDS

    async def run(self, credentials: ServiceCredentials) -> TestResult:
        if not credentials.secrets.get("access_key") and not credentials.secrets.get("secret_key"):
            # See the module docstring. NOT a failure and not a check: this
            # deployment is not uploading raw audio through the platform, and
            # spec 16.2 makes the service conditionally required for exactly
            # this case. E5.5 records it on the row so the rollup agrees.
            return TestResult(
                service_key=self.service_key,
                outcome="not_required",
                checks=(
                    CheckResult(
                        name="configured",
                        passed=True,
                        detail=(
                            "no object storage credentials are set, so this deployment is not "
                            "uploading raw audio through the platform; spec 16.2 makes object "
                            "storage optional and this does not hold the deployment back"
                        ),
                        remedy="",
                        elapsed_ms=0,
                    ),
                ),
            )

        try:
            client = client_for(credentials)
        except (KeyError, TypeError, ValueError) as error:
            return TestResult(
                service_key=self.service_key,
                outcome="fail",
                checks=(
                    CheckResult(
                        name="settings",
                        passed=False,
                        detail=f"the stored object storage settings are incomplete "
                        f"({type(error).__name__})",
                        remedy=(
                            "re-enter the bucket, endpoint and keys, save them, then test again"
                        ),
                        elapsed_ms=0,
                    ),
                ),
            )

        checks: list[CheckResult] = []
        started = time.monotonic()
        try:
            await client.head_bucket()
        except Exception as error:  # noqa: BLE001  (classified, never re-raised)
            failure = client.classify(error)
            return TestResult(
                service_key=self.service_key,
                outcome="fail",
                checks=(
                    CheckResult(
                        name="head_bucket",
                        passed=False,
                        detail=failure.detail,
                        remedy=failure.remedy,
                        elapsed_ms=_ms(started),
                    ),
                ),
            )
        checks.append(
            CheckResult(
                name="head_bucket",
                passed=True,
                detail=f"{client} exists and these credentials may use it",
                remedy="",
                elapsed_ms=_ms(started),
            )
        )

        write_started = time.monotonic()
        wrote = False
        try:
            await client.put_probe_object()
            wrote = True
            checks.append(
                CheckResult(
                    name="write",
                    passed=True,
                    detail=f"wrote a zero-byte object at '{SELFTEST_KEY}'",
                    remedy="",
                    elapsed_ms=_ms(write_started),
                )
            )
        except Exception as error:  # noqa: BLE001
            failure = client.classify(error)
            checks.append(
                CheckResult(
                    name="write",
                    passed=False,
                    detail=failure.detail,
                    remedy=(
                        "grant this key s3:PutObject on the bucket. The Aggregators upload raw "
                        "audio with it, so a key that can only read is not enough"
                        if failure.kind == "forbidden"
                        else failure.remedy
                    ),
                    elapsed_ms=_ms(write_started),
                )
            )

        cleanup_started = time.monotonic()
        try:
            if wrote:
                await client.delete_probe_object()
            remaining = await client.list_probe_prefix()
        except Exception as error:  # noqa: BLE001
            failure = client.classify(error)
            checks.append(
                CheckResult(
                    name="cleanup",
                    passed=False,
                    detail=failure.detail,
                    remedy=(
                        f"the platform wrote a test object at '{SELFTEST_KEY}' and could not "
                        "remove it. Delete it by hand and grant this key s3:DeleteObject and "
                        "s3:ListBucket"
                    ),
                    elapsed_ms=_ms(cleanup_started),
                )
            )
        else:
            checks.append(
                CheckResult(
                    name="cleanup",
                    passed=not remaining,
                    detail=(
                        f"the reserved prefix '{SELFTEST_PREFIX}' is empty"
                        if not remaining
                        else (
                            f"the reserved prefix '{SELFTEST_PREFIX}' still holds "
                            f"{len(remaining)} objects"
                        )
                    ),
                    remedy=(
                        ""
                        if not remaining
                        else (
                            f"delete the objects under '{SELFTEST_PREFIX}' in the bucket "
                            f"'{client.bucket}'; they are the platform's own test data and it "
                            "could not remove them"
                        )
                    ),
                    elapsed_ms=_ms(cleanup_started),
                )
            )

        return TestResult(
            service_key=self.service_key,
            outcome="pass" if all(check.passed for check in checks) else "fail",
            checks=tuple(checks),
        )
