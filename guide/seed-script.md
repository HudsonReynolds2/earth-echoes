# The Seed Script

`app.seed` creates the platform's first account: an organization-wide **owner** with full
administrative access. It exists because a fresh platform has no accounts at all, and every
other way of creating one requires being signed in.

## Usage

```
cd backend
uv run python -m app.seed
```

Requirements: `DATABASE_URL` (and the other required variables from `deploy/.env.example`)
present in the environment. When your stack is running via Docker Compose, point
`DATABASE_URL` at `localhost:5432` with the same credentials you configured.

Optional: `EOE_SEED_OWNER_EMAIL` overrides the account email
(default `owner@example.com`).

### Demo hierarchy (optional)

```
uv run python -m app.seed --demo
```

Adds a realistic demo inventory alongside the owner: the "Earth Echoes Demo" organization
with two deployments (Redwood Coast, High Desert), six pods with their aggregators, and 28
listeners. Run it on a fresh database (owner + hierarchy in one command) or after a plain
seed (hierarchy only — your owner and password are untouched). It refuses to run twice.
The contents are fixed and documented in `docs/INTERFACES.md` ("The demo fixture"), so
demos, tests, and tutorials can rely on the same names every time.

## Exactly what it does

1. **Runs all database migrations to head.** On a completely empty database this creates
   the full schema — the script is safe as the very first thing you ever run.
2. **Checks for an existing organization-wide owner.** If one exists, the script refuses,
   prints which account it found, and exits with status 1. It never modifies existing
   accounts.
3. **Creates the owner** with a cryptographically random 16-byte password, stored only as
   an Argon2id hash.
4. **Writes an audit record** (`user.create`, actor "system") — the account's creation is
   permanently visible in the platform's audit log.
5. **Prints the credentials exactly once** and exits.

## Implications you should understand

- **The password is displayed once and exists nowhere else.** Not in the database (only
  its hash), not in any file, not in any log. If you lose it before recording it, nothing
  can recover it — but nothing is broken either: run the script against a fresh database,
  or if other owner accounts exist, have one of them set a new password via the Users
  admin page.
- **Re-running is refused by design.** The script is a bootstrap tool, not an account
  manager. Day-two account management happens signed-in, under audit, through the Users
  page or `PATCH /api/v1/users/{id}`.
- **The account is a real owner.** It can create and deactivate accounts, assign any role,
  and read the audit log. Treat its credentials accordingly; consider enabling two-factor
  authentication (Sign in → enroll TOTP) immediately.
- **The audit trail starts here.** The seed run itself is the first entry in the immutable
  audit log; everything the account does afterwards is recorded with its identity.
