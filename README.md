# Echoes of Earth Management Platform

Web platform for deployment configuration, remote monitoring, and remote reconfiguration of
the Echoes of Earth bioacoustic monitoring system. The authoritative specification and the
phase documents live in `project_planning/`; the binding working rules live in `CLAUDE.md`
and `.claude/rules/project-rules.json`; the growing inter-phase contract lives in
`docs/INTERFACES.md`.

Repository layout (fixed by phase-0-foundations.md section 2):

```
/backend        FastAPI app (package name: app), alembic/, tests/
/frontend       Vite React TS app
/deploy         docker-compose.yml and env templates
/sim            reserved for the simulation harness (SIM epic)
/docs           INTERFACES.md, DECISIONS.md, project logs
```

## Development setup

1. Install Docker Desktop, Node 20+, uv, and git.
2. Clone the repository and open a terminal at its root.
3. Copy `deploy/.env.example` to `deploy/.env` and fill in every value (the file documents
   each one; never commit `.env`).
4. Run `docker compose -f deploy/docker-compose.yml up -d --build`.
5. Seed the initial owner: `cd backend` then `uv run python -m app.seed` (with
   `DATABASE_URL` pointing at the stack's Postgres); the credentials print exactly once.
6. Open the API at `http://localhost:8000` and the frontend at `http://localhost:5173`,
   and sign in with the seeded credentials.
7. For backend work outside containers: `cd backend` then `uv sync`.
8. For frontend work outside containers: `cd frontend` then `npm ci`, then `npm run dev`.
9. Before finishing any task, run the gate: `./gate.ps1` (Windows) or `make gate` (POSIX);
   the entire suite must pass (see `CLAUDE.md`, rule R0).
10. Read `docs/INTERFACES.md` and `docs/DECISIONS.md` before changing anything they cover.

Dev ports: API 8000, frontend 5173, Postgres 5432, Redis 6379.
