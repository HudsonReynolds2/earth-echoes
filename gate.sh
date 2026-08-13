#!/bin/sh
# R0 gate runner AND the canonical CI stage registry
# (.claude/rules/project-rules.json; docs/INTERFACES.md section "CI pipeline").
#
# Usage:
#   sh gate.sh              run every applicable local stage (the full gate)
#   sh gate.sh <stage>      run exactly one stage (what each CI job invokes)
#   sh gate.sh --list       print the registry, one stage per line
#
# CI jobs call single stages, so CI and the local gate execute the same code.
# To add a stage: add its function and STAGES entry here, add a job invoking
# it in .github/workflows/ci.yml, and add that job to the ci-green needs
# list. backend/tests/test_ci_pipeline.py enforces registry/workflow parity.
# EOE_GATE=1 makes any skipped, xfailed, or deselected test a hard failure.
set -u
export EOE_GATE=1
root="$(cd "$(dirname "$0")" && pwd)"

# Registry. Naming: <area>-<kind>. LOCAL_STAGES is the no-argument run;
# migrations-check needs a caller-supplied DATABASE_URL and containers-build
# duplicates what the backend-tests integration suite already builds, so both
# are CI-first stages that stay individually invocable here.
STAGES="backend-quality backend-tests migrations-check frontend-quality frontend-tests frontend-e2e sim-quality sim-protocol containers-build"
LOCAL_STAGES="backend-quality backend-tests frontend-quality frontend-tests frontend-e2e sim-quality sim-protocol"

stage_backend_quality() {
    cd "$root/backend" || return 1
    uv run ruff check . || return 1
    uv run ruff format --check . || return 1
    if [ -d app ]; then
        uv run mypy app || return 1
    fi
}

stage_backend_tests() {
    cd "$root/backend" || return 1
    uv run python tests/gate_runner.py
}

stage_migrations_check() {
    # The literal reversibility commands deferred by DECISIONS D9. DATABASE_URL
    # must point at a disposable database (CI uses a service container).
    cd "$root/backend" || return 1
    uv run alembic upgrade head || return 1
    uv run alembic downgrade -1 || return 1
    uv run alembic upgrade head || return 1
    uv run alembic downgrade base || return 1
    uv run alembic upgrade head
}

stage_frontend_quality() {
    cd "$root/frontend" || return 1
    npm run --silent lint || return 1
    npm run --silent typecheck
}

stage_frontend_tests() {
    cd "$root/frontend" || return 1
    npm run --silent test
}

stage_frontend_e2e() {
    cd "$root/frontend" || return 1
    npm run --silent test:e2e
}

stage_sim_quality() {
    # `/sim` is its own uv project with its own venv (D100), so every command
    # here runs from that directory. mypy is configured through `files` in
    # sim/pyproject.toml, which is why the invocation is bare.
    cd "$root/sim" || return 1
    uv run ruff check . || return 1
    uv run ruff format --check . || return 1
    uv run mypy
}

stage_sim_protocol() {
    # The harness suite against a real Mosquitto and a real platform. Its runner
    # is sim/tests/gate_runner.py, which imports GateGuard/enforce from the
    # backend's — one implementation of R0, launched from two projects.
    cd "$root/sim" || return 1
    uv run python tests/gate_runner.py
}

stage_containers_build() {
    docker build --build-arg BUILD_SHA="${BUILD_SHA:-dev}" "$root/backend" || return 1
    docker build --target dev "$root/frontend" || return 1
    docker build --target prod "$root/frontend"
}

is_stage_applicable() {
    case "$1" in
        backend-*|migrations-*) [ -f "$root/backend/pyproject.toml" ] ;;
        frontend-e2e) [ -f "$root/frontend/playwright.config.ts" ] ;;
        frontend-*) [ -f "$root/frontend/package.json" ] ;;
        sim-*) [ -f "$root/sim/pyproject.toml" ] ;;
        *) true ;;
    esac
}

run_stage() {
    printf '\n== %s ==\n' "$1"
    ( "stage_$(printf '%s' "$1" | tr '-' '_')" )
}

if [ "${1:-}" = "--list" ]; then
    for stage in $STAGES; do echo "$stage"; done
    exit 0
fi

if [ -n "${1:-}" ]; then
    case " $STAGES " in
        *" $1 "*) ;;
        *) echo "unknown stage: $1 (see: sh gate.sh --list)" >&2; exit 2 ;;
    esac
    run_stage "$1"
    exit $?
fi

failures=""
for stage in $LOCAL_STAGES; do
    if is_stage_applicable "$stage"; then
        if ! run_stage "$stage"; then
            failures="$failures, $stage"
        fi
    fi
done

echo
if [ -n "$failures" ]; then
    echo "GATE RED:${failures#,}"
    exit 1
fi
echo "GATE GREEN: entire accumulated suite passed (0 skipped, 0 xfailed, 0 deselected)"
exit 0
