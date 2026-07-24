#!/bin/sh
# R0 gate runner, POSIX mirror of gate.ps1 (.claude/rules/project-rules.json).
# Runs the ENTIRE accumulated suite for every stack that exists at this gate.
# EOE_GATE=1 makes any skipped, xfailed, or deselected test a hard failure.
set -u
export EOE_GATE=1
root="$(cd "$(dirname "$0")" && pwd)"
failures=""

stage() {
    name="$1"
    shift
    printf '\n== %s ==\n' "$name"
    if ! "$@"; then
        failures="$failures, $name"
    fi
}

if [ -f "$root/backend/pyproject.toml" ]; then
    cd "$root/backend"
    stage "backend: ruff check" uv run ruff check .
    stage "backend: ruff format check" uv run ruff format --check .
    if [ -d app ]; then
        stage "backend: mypy" uv run mypy app
    fi
    stage "backend: pytest (entire suite)" uv run python tests/gate_runner.py
    cd "$root"
fi

if [ -f "$root/frontend/package.json" ]; then
    cd "$root/frontend"
    stage "frontend: eslint" npm run --silent lint
    stage "frontend: typecheck" npm run --silent typecheck
    stage "frontend: vitest (entire suite)" npm run --silent test
    if [ -f playwright.config.ts ]; then
        stage "frontend: playwright" npm run --silent test:e2e
    fi
    cd "$root"
fi

echo
if [ -n "$failures" ]; then
    echo "GATE RED:${failures#,}"
    exit 1
fi
echo "GATE GREEN: entire accumulated suite passed (0 skipped, 0 xfailed, 0 deselected)"
exit 0
