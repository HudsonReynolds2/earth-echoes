# The simulation harness container (task SIM.4), run behind the `sim` compose
# profile so an ordinary `docker compose up` never starts a fleet.
#
# **The only platform code in this image is the published wire contract.** The
# harness boundary rule — a client of the platform, never a part of it — is
# asserted over the source tree by `sim/tests/test_harness_boundaries.py`; here
# it is made true of the filesystem, which is a stronger statement: an import of
# `app.devbroker` or `app.models` from inside this container cannot fail a test,
# it fails outright, because those files are not here.
#
# The dependency set is a DEVICE's, for the reason `sim/pyproject.toml` gives:
# `--no-dev` leaves aiomqtt and pydantic and nothing else, so this image is
# firmware-shaped rather than platform-shaped and cannot quietly acquire a
# database driver.
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv/sim
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

COPY sim/pyproject.toml sim/uv.lock ./
RUN uv sync --frozen --no-dev

# The published contract, at the path `device.py` computes (`../backend` beside
# `/srv/sim`). Two files and one package marker: nothing else from the platform
# is reachable from this container at all.
COPY backend/app/__init__.py /srv/backend/app/__init__.py
COPY backend/app/contracts /srv/backend/app/contracts

COPY sim/checksum.py sim/device.py sim/fleet.py sim/provision.py sim/scenarios.py ./
COPY sim/scenarios ./scenarios

# Production posture (D19), and it matters more than usual here: this container
# reads `accounts.json` off a read-only mount, and a root process with broker
# passwords in it is a worse dev artefact than a non-root one.
RUN useradd --uid 10001 --create-home eoe && chown -R eoe:eoe /srv
USER eoe

# The runner is the entrypoint, so `docker compose run sim --list-scenarios`
# and the compose service's own `command:` are the same kind of thing: flags.
ENTRYPOINT ["/srv/sim/.venv/bin/python", "fleet.py"]
CMD ["--stay", "--no-provision", "--no-apply"]
