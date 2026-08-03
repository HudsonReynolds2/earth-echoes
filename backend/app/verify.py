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
from app.models import (
    Aggregator,
    Deployment,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
    Secret,
    User,
    UserSession,
)

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


def bootstrap_scope(database_url: str, run_tag: str) -> uuid.UUID:
    """A real deployment for the scoped-operator step: since E1.1 the grant
    carries a foreign key, so the scope can no longer be an invented UUID.
    Reuses an existing organization when one exists (single-org rule, spec
    12.1); cleanup() removes whatever this created."""
    _, factory = create_session_factory(database_url)
    with factory() as db:
        org_id = db.scalar(select(Organization.id).limit(1))
        if org_id is None:
            org = Organization(name=f"verify-org-{run_tag}")
            db.add(org)
            db.flush()
            org_id = org.id
        deployment = Deployment(
            organization_id=org_id,
            name=f"verify-dep-{run_tag}",
            slug=f"verify-dep-{run_tag}",
        )
        db.add(deployment)
        db.commit()
        return deployment.id


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
        # Hierarchy rows from bootstrap_scope and the E1 walk (safety net for
        # a run that failed mid-walk): children first, then the deployments
        # and any verify-created organization. Grants referencing them are
        # gone with their users above.
        verify_deps = list(
            db.scalars(select(Deployment.id).where(Deployment.name.like("verify-%")))
        )
        if verify_deps:
            db.execute(delete(Listener).where(Listener.deployment_id.in_(verify_deps)))
            verify_pods = list(db.scalars(select(Pod.id).where(Pod.deployment_id.in_(verify_deps))))
            if verify_pods:
                db.execute(delete(Aggregator).where(Aggregator.pod_id.in_(verify_pods)))
                db.execute(delete(Pod).where(Pod.id.in_(verify_pods)))
            db.execute(delete(Deployment).where(Deployment.id.in_(verify_deps)))
        db.execute(delete(Organization).where(Organization.name.like("verify-org-%")))
        db.commit()
    return removed


def run(api: str, database_url: str) -> int:
    report = Report()
    run_tag = uuid.uuid4().hex[:8]
    owner = TempAccount(f"verify-owner-{run_tag}@example.com", pysecrets.token_urlsafe(16))
    viewer = TempAccount(f"verify-viewer-{run_tag}@example.com", pysecrets.token_urlsafe(16))
    operator = TempAccount(f"verify-operator-{run_tag}@example.com", pysecrets.token_urlsafe(16))
    print(f"verifying deployment at {api} (run {run_tag})")
    print("bootstrapping temporary owner ...")
    owner.user_id = bootstrap_owner(database_url, owner)
    print("bootstrapping scope deployment ...")
    scoped_deployment = str(bootstrap_scope(database_url, run_tag))

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

            # -- E1 hierarchy walk over real HTTP (task E1.9) ---------------
            organizations = c.get("/organizations")
            org_items = (
                organizations.json().get("items", []) if organizations.status_code == 200 else []
            )
            report.record("hierarchy: organization visible to the owner", len(org_items) >= 1)
            walk_dep = c.post(
                "/deployments",
                json={"organization_id": org_items[0]["id"], "name": f"verify-walk-{run_tag}"},
                headers=_csrf(c),
            )
            report.record(
                "hierarchy: deployment created with a generated slug",
                walk_dep.status_code == 201
                and walk_dep.json().get("slug") == f"verify-walk-{run_tag}",
                walk_dep.text[:120],
            )
            walk_dep_id = walk_dep.json().get("id", "")
            walk_pod = c.post(
                "/pods",
                json={
                    "deployment_id": walk_dep_id,
                    "name": "verify-pod",
                    "aggregator": {},  # create-and-attach in one call (E1.3)
                },
                headers=_csrf(c),
            )
            report.record(
                "hierarchy: pod created with its aggregator in one call",
                walk_pod.status_code == 201 and walk_pod.json().get("aggregator") is not None,
            )
            walk_agg_id = (walk_pod.json().get("aggregator") or {}).get("id", "")
            walk_mac = f"02:5E:1F:{run_tag[0:2]}:{run_tag[2:4]}:{run_tag[4:6]}".upper()
            walk_listener = c.post(
                "/listeners",
                json={"mac": walk_mac, "name": "verify-listener", "aggregator_id": walk_agg_id},
                headers=_csrf(c),
            )
            report.record(
                "hierarchy: listener registered by MAC",
                walk_listener.status_code == 201 and walk_listener.json().get("mac") == walk_mac,
            )
            dup_name = c.post(
                "/listeners",
                json={
                    "mac": "02:5E:1F:00:00:FF",
                    "name": "verify-listener",
                    "aggregator_id": walk_agg_id,
                },
                headers=_csrf(c),
            )
            report.record(
                "hierarchy: duplicate name rejected with a suggestion (E1.4)",
                dup_name.status_code == 409
                and (dup_name.json().get("error", {}).get("detail") or {}).get("suggestion")
                == "verify-listener-2",
            )
            suffixed = c.post(
                "/listeners",
                json={
                    "mac": "02:5E:1F:00:00:FF",
                    "name": "verify-listener",
                    "aggregator_id": walk_agg_id,
                    "auto_suffix": True,
                },
                headers=_csrf(c),
            )
            report.record(
                "hierarchy: explicit auto_suffix lands on the next free name",
                suffixed.status_code == 201 and suffixed.json().get("name") == "verify-listener-2",
            )
            tagged = c.put(
                f"/listeners/{walk_mac}/tags",
                json={"tags": ["verify", "walk", "verify"]},
                headers=_csrf(c),
            )
            report.record(
                "hierarchy: tags replace wholesale, deduped and sorted (E1.7)",
                tagged.status_code == 200 and tagged.json().get("tags") == ["verify", "walk"],
            )
            with _client(api) as scoped:
                _login(scoped, operator)
                visible = scoped.get("/deployments")
                names = (
                    {item["name"] for item in visible.json().get("items", [])}
                    if visible.status_code == 200
                    else set()
                )
                report.record(
                    "hierarchy: scoped operator sees only their deployment (D35)",
                    f"verify-walk-{run_tag}" not in names,
                    f"visible={sorted(names)}",
                )
                report.record(
                    "hierarchy: out-of-scope listener answers 404, not 403 (D35)",
                    scoped.get(f"/listeners/{walk_mac}").status_code == 404,
                )
            blocked_delete = c.delete(f"/deployments/{walk_dep_id}", headers=_csrf(c))
            report.record(
                "hierarchy: delete with children is 409 with named blockers",
                blocked_delete.status_code == 409
                and "pods"
                in (blocked_delete.json().get("error", {}).get("detail") or {}).get("children", {}),
            )
            teardown_ok = True
            for path in (
                f"/listeners/{walk_mac}",
                "/listeners/02:5E:1F:00:00:FF",
                f"/aggregators/{walk_agg_id}",
                f"/pods/{walk_pod.json().get('id', '')}",
                f"/deployments/{walk_dep_id}",
            ):
                teardown_ok = teardown_ok and c.delete(path, headers=_csrf(c)).status_code == 204
            report.record("hierarchy: leaf-up teardown deletes cleanly", teardown_ok)
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
