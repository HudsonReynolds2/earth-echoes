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

The control-plane broker needs its TLS material before anything starts — it is generated,
never committed, so a fresh clone has none:

```
cd backend && uv run python -m app.devbroker --certs-only && cd ..
```

```
docker compose -f deploy/docker-compose.yml up -d --build
```

Five services start: the API (host port 18000), the web frontend (15173), PostgreSQL
(15432), Redis (16379), and the Mosquitto control-plane broker (18883, TLS only). The
published host ports sit in the 1xxxx range so the stack coexists with other services you
may already run; inside the compose network each container still uses its standard port.
All five carry health checks; `docker compose ps` shows them healthy within about a
minute on first build.

Once deployments exist (step 3 onward), run `uv run python -m app.devbroker --host mosquitto
--keep-tls` from `backend/` and restart the broker to mint its per-deployment and per-device
accounts. The repository README's "Development MQTT broker" section explains why this takes
two passes.

## 3. Create the owner account

```
cd backend
uv run python -m app.seed
```

The credentials print **once**. Record them immediately — see
[the seed script guide](seed-script.md) for exactly what this does and why the password
can never be shown again.

## 4. Sign in

Open `http://localhost:15173`, choose **Sign in**, and use the seeded credentials. As the
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
  `http://localhost:18000` by default; if your API is elsewhere, set `EOE_FRONTEND_API_URL`
  in `deploy/.env` and restart the frontend service.
