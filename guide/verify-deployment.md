# Verify Your Deployment

`app.verify` proves that every subsystem of the platform is actually working on **your**
deployment — not in a lab, but against the stack you just launched. It creates a temporary
owner account, exercises the entire platform surface through it over real HTTP, prints a
per-check PASS/FAIL report, and then deletes every account it created.

## Usage

```
cd backend
uv run python -m app.verify                       # against http://localhost:18000
uv run python -m app.verify --api https://host    # against any reachable deployment
```

Requirements:
- The API must be reachable at the given URL.
- `DATABASE_URL` must reach the deployment's PostgreSQL. Creating the very first account
  and deleting temporary ones are deliberate database-level operations — the platform's
  API has no account-delete endpoint by design.

Exit status: `0` when every check passes; `1` when any check fails; `2` for missing
configuration.

## What it checks

| Area | Checks |
|---|---|
| Health | API up; API can reach its database |
| Authentication | wrong password rejected; owner login; session identity via `/auth/me` |
| CSRF | mutations without the token are rejected |
| Two-factor | full TOTP lifecycle: enroll, confirm, login blocked without a code, login with a live code |
| User administration | owner creates a viewer and a deployment-scoped operator |
| Role enforcement | both created roles can sign in and are denied administration and audit access |
| Audit | the owner's trail shows the run's logins, account creations, and TOTP events |
| Session revocation | deactivating an account kills its live session immediately |

## Implications you should understand

- **Temporary accounts are real accounts while they exist** (a few seconds):
  `verify-owner-…`, `verify-viewer-…`, `verify-operator-…` at `@example.com`. Their
  passwords are random per run and are never printed or stored.
- **Cleanup is guaranteed** — sessions, role assignments, and TOTP secrets for those
  accounts are removed even if checks fail (the cleanup runs in a `finally`).
- **The audit log keeps the evidence.** The platform's audit log is immutable, so the
  verification run's entries remain permanently, with their actor reference cleared when
  the temporary account is deleted. This is intentional: an audit log you can scrub is not
  an audit log. Expect `auth.login`, `user.create`, and `auth.totp_*` entries from each
  run.
- **Safe to run repeatedly**, including on a production deployment: it touches only its
  own temporary accounts and never modifies existing data.
