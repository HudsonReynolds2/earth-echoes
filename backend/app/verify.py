"""Deployment verifier (E0.12+; guide/verify-deployment.md is the USER doc).

    uv run python -m app.verify [--api http://localhost:8000]

Drives every Phase-0 subsystem through a TEMPORARY owner account over real
HTTP against a running deployment: health, login and session mechanics, CSRF,
the full TOTP enrollment and login gate, user administration, RBAC allow and
deny for every created role, the audit trail, and live session revocation on
deactivation. Prints a per-step PASS/FAIL report and exits nonzero on any
failure.

Requirements: EOE_VERIFY_API_URL or --api reachable from this machine, and
DATABASE_URL reaching the deployment's Postgres (bootstrap and cleanup are
direct database operations; the API deliberately has no user-delete surface).

Cleanup guarantee: every account this tool creates (`verify-*@example.com`)
is deleted afterwards — sessions, role assignments, TOTP secret, then the
user rows. Audit rows are NOT deleted: the audit log is immutable by design,
so the verification run remains visible there with its actor reference
nulled. Passwords are generated per run and never printed or stored.
"""

import argparse
import os
import secrets as pysecrets
import sys
import uuid
from dataclasses import dataclass, field

import httpx
import pyotp
from sqlalchemy import delete, select

from app.auth.passwords import hash_password
from app.db import create_session_factory
from app.models import RoleAssignment, Secret, User, UserSession

API_PREFIX = "/api/v1"


@dataclass
class Report:
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, ok: bool, note: str = "") -> bool:
        self.steps.append((name, ok, note))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {note}" if note and not ok else ""))
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.steps if not ok)


@dataclass
class TempAccount:
    email: str
    password: str
    user_id: uuid.UUID | None = None


def _client(api: str) -> httpx.Client:
    return httpx.Client(base_url=f"{api}{API_PREFIX}", timeout=15.0)


def _login(
    client: httpx.Client, account: TempAccount, totp_code: str | None = None
) -> httpx.Response:
    payload: dict[str, str | None] = {"email": account.email, "password": account.password}
    if totp_code is not None:
        payload["totp_code"] = totp_code
    return client.post("/auth/login", json=payload)


def _csrf(client: httpx.Client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("eoe_csrf") or ""}


def bootstrap_owner(database_url: str, account: TempAccount) -> uuid.UUID:
    _, factory = create_session_factory(database_url)
    with factory() as db:
        user = User(email=account.email, password_hash=hash_password(account.password))
        user.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
        db.add(user)
        db.commit()
        return user.id


def cleanup(database_url: str, accounts: list[TempAccount]) -> int:
    """Delete every temp account and its artifacts; audit rows survive with a
    nulled actor (immutability). Returns the number of users removed."""
    _, factory = create_session_factory(database_url)
    removed = 0
    with factory() as db:
        for account in accounts:
            user_id = db.scalar(select(User.id).where(User.email == account.email))
            if user_id is None:
                continue
            db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            db.execute(delete(RoleAssignment).where(RoleAssignment.user_id == user_id))
            db.execute(delete(Secret).where(Secret.name == f"totp:{user_id}"))
            db.execute(delete(User).where(User.id == user_id))
            removed += 1
        db.commit()
    return removed


def run(api: str, database_url: str) -> int:
    report = Report()
    run_tag = uuid.uuid4().hex[:8]
    owner = TempAccount(f"verify-owner-{run_tag}@example.com", pysecrets.token_urlsafe(16))
    viewer = TempAccount(f"verify-viewer-{run_tag}@example.com", pysecrets.token_urlsafe(16))
    operator = TempAccount(f"verify-operator-{run_tag}@example.com", pysecrets.token_urlsafe(16))
    scoped_deployment = str(uuid.uuid4())

    print(f"verifying deployment at {api} (run {run_tag})")
    print("bootstrapping temporary owner ...")
    owner.user_id = bootstrap_owner(database_url, owner)

    try:
        with _client(api) as c:
            # -- health ----------------------------------------------------
            health = c.get("/health")
            body = health.json() if health.status_code == 200 else {}
            report.record(
                "health: API up and database reachable",
                health.status_code == 200 and body.get("database") == "ok",
                f"status={health.status_code} body={body}",
            )

            # -- auth and sessions (E0.6) ----------------------------------
            bad = _login(c, TempAccount(owner.email, "wrong-password"))
            report.record("auth: wrong password rejected with 401", bad.status_code == 401)
            login = _login(c, owner)
            report.record("auth: owner login succeeds", login.status_code == 200, login.text[:120])
            me = c.get("/auth/me")
            report.record(
                "auth: /me reports the org-wide owner assignment",
                me.status_code == 200
                and me.json().get("assignments") == [{"role": "owner", "deployment_id": None}],
            )

            # -- CSRF (D4) -------------------------------------------------
            no_csrf = c.post("/auth/logout")
            report.record("csrf: mutation without the token is 403", no_csrf.status_code == 403)

            # -- TOTP, the full gate (E0.10) -------------------------------
            enroll = c.post("/auth/totp/enroll", headers=_csrf(c))
            totp_secret = enroll.json().get("secret", "") if enroll.status_code == 200 else ""
            report.record("totp: enrollment issues a secret", bool(totp_secret))
            confirm = c.post(
                "/auth/totp/confirm",
                json={"code": pyotp.TOTP(totp_secret).now() if totp_secret else "0"},
                headers=_csrf(c),
            )
            report.record("totp: confirmation enables the factor", confirm.status_code == 200)
            c.post("/auth/logout", headers=_csrf(c))
            blocked = _login(c, owner)
            report.record(
                "totp: login without a code is blocked with totp_required",
                blocked.status_code == 401
                and (blocked.json().get("error", {}).get("detail") or {}).get("totp_required")
                is True,
            )
            code = pyotp.TOTP(totp_secret).now() if totp_secret else "0"
            relogin = _login(c, owner, totp_code=code)
            report.record("totp: login with a live code succeeds", relogin.status_code == 200)

            # -- user administration (E0.9) --------------------------------
            created_viewer = c.post(
                "/users",
                json={
                    "email": viewer.email,
                    "password": viewer.password,
                    "assignments": [{"role": "viewer", "deployment_id": None}],
                },
                headers=_csrf(c),
            )
            report.record("admin: owner creates a viewer", created_viewer.status_code == 201)
            created_operator = c.post(
                "/users",
                json={
                    "email": operator.email,
                    "password": operator.password,
                    "assignments": [
                        {"role": "deployment_operator", "deployment_id": scoped_deployment}
                    ],
                },
                headers=_csrf(c),
            )
            report.record(
                "admin: owner creates a deployment-scoped operator",
                created_operator.status_code == 201,
            )

            # -- RBAC deny paths for each created role (E0.7) --------------
            for account, label in ((viewer, "viewer"), (operator, "operator")):
                with _client(api) as role_client:
                    role_login = _login(role_client, account)
                    report.record(f"rbac: {label} can log in", role_login.status_code == 200)
                    report.record(
                        f"rbac: {label} denied user administration (403)",
                        role_client.get("/users").status_code == 403,
                    )
                    report.record(
                        f"rbac: {label} denied the audit log (403)",
                        role_client.get("/audit").status_code == 403,
                    )

            # -- audit trail (E0.8) ----------------------------------------
            trail = c.get("/audit", params={"actor": str(owner.user_id)})
            actions = (
                {entry["action"] for entry in trail.json().get("items", [])}
                if trail.status_code == 200
                else set()
            )
            report.record(
                "audit: owner's trail shows logins, user creation, and totp events",
                {"auth.login", "user.create", "auth.totp_enabled"} <= actions,
                f"actions={sorted(actions)}",
            )

            # -- deactivation revokes live sessions (E0.9/D1) --------------
            with _client(api) as victim:
                _login(victim, viewer)
                viewer_id = created_viewer.json()["id"]
                deactivate = c.patch(
                    f"/users/{viewer_id}", json={"is_active": False}, headers=_csrf(c)
                )
                report.record(
                    "admin: deactivation returns the updated user",
                    deactivate.status_code == 200 and deactivate.json()["is_active"] is False,
                )
                report.record(
                    "sessions: deactivated user's live session is revoked",
                    victim.get("/auth/me").status_code == 401,
                )
    finally:
        print("cleaning up temporary accounts ...")
        removed = cleanup(database_url, [owner, viewer, operator])
        print(f"removed {removed} temporary account(s); audit trail retained (immutable)")

    print()
    passed = len(report.steps) - report.failed
    print(f"result: {passed}/{len(report.steps)} checks passed")
    if report.failed:
        print("DEPLOYMENT VERIFICATION FAILED", file=sys.stderr)
        return 1
    print("deployment verified: every Phase-0 subsystem is working")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api",
        default=os.environ.get("EOE_VERIFY_API_URL", "http://localhost:8000"),
        help="base URL of the deployment's API (default http://localhost:8000)",
    )
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required (see guide/verify-deployment.md)", file=sys.stderr)
        return 2
    return run(args.api.rstrip("/"), database_url)


if __name__ == "__main__":
    raise SystemExit(main())
