# Getting Started

Everything needed to take a fresh machine to a running, signed-in platform.

## Prerequisites

- **Docker Desktop** (or Docker Engine + Compose v2) — runs the platform stack
- **uv** (https://docs.astral.sh/uv/) — runs the seed and verification tools
- **git**

## 1. Configure

```
cp deploy/.env.example deploy/.env
```

Open `deploy/.env` and fill in **every** value. Each variable is documented in the file
itself. Three of them are secrets you generate:

```
openssl rand -base64 32     # use separate outputs for EOE_SESSION_SECRET and EOE_KEK
openssl rand -hex 16        # a fine POSTGRES_PASSWORD
```

**`EOE_KEK` matters more than the others**: it is the key-encryption key protecting every
secret the platform stores (service credentials, TOTP secrets, and later the device
provisioning secrets). Losing it makes stored secrets unrecoverable; leaking it undermines
their encryption. Store it in a password manager or secret manager, not just in `.env`.

## 2. Launch

```
docker compose -f deploy/docker-compose.yml up -d --build
```

Four services start: the API (port 8000), the web frontend (5173), PostgreSQL (5432), and
Redis (6379). All four carry health checks; `docker compose ps` shows them healthy within
about a minute on first build.

## 3. Create the owner account

```
cd backend
uv run python -m app.seed
```

The credentials print **once**. Record them immediately — see
[the seed script guide](seed-script.md) for exactly what this does and why the password
can never be shown again.

## 4. Sign in

Open `http://localhost:5173`, choose **Sign in**, and use the seeded credentials. As the
owner you can create further accounts under **Users**, assign roles, and (optionally)
enable two-factor authentication for your account.

## 5. Verify (recommended)

```
uv run python -m app.verify
```

Runs a complete end-to-end check of every platform subsystem using a temporary account,
then removes it. See [Verify your deployment](verify-deployment.md).

## Troubleshooting

- **A service never turns healthy** — `docker compose logs <service>`. The API refuses to
  start if a required variable is missing from `deploy/.env`; the error names the variable.
- **`EOE_KEK must decode to 32 bytes`** — the KEK must be base64 of exactly 32 random
  bytes; regenerate with `openssl rand -base64 32`.
- **The frontend loads but shows "API unreachable"** — the browser reaches the API at
  `http://localhost:8000` by default; if your API is elsewhere, set `EOE_FRONTEND_API_URL`
  in `deploy/.env` and restart the frontend service.
