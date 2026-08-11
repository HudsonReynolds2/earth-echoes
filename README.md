# Echoes of Earth Management Platform

> **Operating a deployment? Start with the [User Guide](guide/README.md)** — quickstart,
> the seed script, and end-to-end deployment verification live there.

Web platform for deployment configuration, remote monitoring, and remote reconfiguration of
the Echoes of Earth bioacoustic monitoring system. The authoritative specification and the
phase documents live in `project_planning/`; the binding working rules live in `CLAUDE.md`
and `.claude/rules/project-rules.json`; the growing inter-phase contract lives in
`docs/INTERFACES.md`.

Repository layout (fixed by phase-0-foundations.md section 2):

```
/guide          USER-FACING: quickstart, seed script, deployment verification
/backend        FastAPI app (package name: app), alembic/, tests/
/frontend       Vite React TS app
/deploy         docker-compose.yml and env templates
/sim            reserved for the simulation harness (SIM epic)
/docs           engineering-internal: INTERFACES.md, DECISIONS.md, project logs
```

## Development setup

1. Install Docker Desktop, Node 20+, uv, and git.
2. Clone the repository and open a terminal at its root.
3. Copy `deploy/.env.example` to `deploy/.env` and fill in every value (the file documents
   each one; never commit `.env`).
4. Generate the dev broker's TLS material — `cd backend` then
   `uv run python -m app.devbroker --certs-only` — and run
   `docker compose -f deploy/docker-compose.yml up -d --build`. Mosquitto will not
   start without those files; see "Development MQTT broker" below.
5. Seed the initial owner: `cd backend` then `uv run python -m app.seed` (with
   `DATABASE_URL` pointing at the stack's Postgres); the credentials print exactly once.
6. Open the API at `http://localhost:18000` and the frontend at `http://localhost:15173`,
   and sign in with the seeded credentials.
7. For backend work outside containers: `cd backend` then `uv sync`.
8. For frontend work outside containers: `cd frontend` then `npm ci`, then `npm run dev`.
9. Before finishing any task, run the gate: `./gate.ps1` (Windows) or `make gate` (POSIX);
   the entire suite must pass (see `CLAUDE.md`, rule R0).
10. Read `docs/INTERFACES.md` and `docs/DECISIONS.md` before changing anything they cover.

Dev ports (host side): API 18000, frontend 15173, Postgres 15432, Redis 16379, MQTT over
TLS 18883. Containers still listen on the standard ports internally; only the published
host ports sit in the 1xxxx range, so the stack coexists with other local services
(project-changes #21).

The stack also runs a `worker` container (`python -m app.controlplane.runner`, E3.7): the
reconciliation loop — the MQTT subscriptions, the pending-revision timeout sweep, and the
periodic drift re-comparison. It publishes no ports and holds no HTTP surface. Set
`EOE_WORKER_IN_API=1` to run it inside the API process instead of as its own container.
Nothing reaches a device until `EOE_PUBLISH_ENABLED` is on (E3.13 flips the default).

## Development MQTT broker

The control plane runs over MQTT (spec section 7). Development uses one Mosquitto
container with a **TLS-only** listener, a private CA, per-deployment platform accounts, and
per-Aggregator device accounts whose broker ACLs cut them to their own topic subtree — the
same shape spec 7.1 mandates in the field, so no code grows a quiet assumption that the
control plane is plaintext or unrestricted.

`app.devbroker` generates all of it into `deploy/dev-certs/`, which is **gitignored**: a
private CA, a server certificate, generated passwords, and the Mosquitto password and ACL
files built from them. Nothing there belongs in a real deployment, and every run rotates
every credential.

It runs in two passes because of a bootstrap order — Mosquitto refuses to start without its
password and ACL files, but the accounts in them come from the database that starts beside
it:

```bash
cd backend && uv run python -m app.devbroker --certs-only
```

```bash
cd backend && uv run python -m app.devbroker --host mosquitto --keep-tls
```

The first pass writes the certificates plus empty account files, so `compose up` succeeds.
The second — after `app.seed --demo` has created deployments — writes one platform account
per deployment, one device account per Aggregator, the matching ACL grants, and the
`deployment_service` row the platform reads its broker coordinates from (the password goes
through `SecretStore`, never into the row). Restart the broker afterwards so it re-reads
the files: `docker compose -f deploy/docker-compose.yml restart mosquitto`.

`--host` is the hostname the **platform** dials: `mosquitto` from inside the compose
network, `localhost` when running the API or worker on the host. Device credentials for
local testing are listed in `deploy/dev-certs/accounts.json`.

On Windows, `.\qa-stack.ps1` does all of this for you.
