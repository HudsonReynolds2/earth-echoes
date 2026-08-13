# R0 gate runner (.claude/rules/project-rules.json): the single gate entry point on
# Windows-native shells. POSIX shells use `make gate`, which mirrors this exactly.
#
# Runs the ENTIRE accumulated suite for every stack that exists at this gate. Sets
# EOE_GATE=1, under which any skipped, xfailed, or deselected test is a hard failure.
# No filter flags, ever: narrowing the gate suite is forbidden by rule R0.

$ErrorActionPreference = "Continue"
$env:EOE_GATE = "1"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$failures = @()

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $winget = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $hit = Get-ChildItem -Path $winget -Recurse -Filter uv.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($hit) { return $hit.FullName }
    throw "uv not found; install with: winget install astral-sh.uv"
}

function Invoke-Stage {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ""
    Write-Host "== $Name ==" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) { $script:failures += $Name }
}

if (Test-Path (Join-Path $root "backend\pyproject.toml")) {
    $uv = Find-Uv
    Push-Location (Join-Path $root "backend")
    try {
        Invoke-Stage "backend: ruff check" { & $uv run ruff check . }
        Invoke-Stage "backend: ruff format check" { & $uv run ruff format --check . }
        if (Test-Path "app") {
            Invoke-Stage "backend: mypy" { & $uv run mypy app }
        }
        Invoke-Stage "backend: pytest (entire suite)" { & $uv run python tests/gate_runner.py }
    }
    finally { Pop-Location }
}

if (Test-Path (Join-Path $root "sim\pyproject.toml")) {
    # The simulation harness (SIM.5). Its own uv project with its own venv
    # (D100), mirroring gate.sh's sim-quality and sim-protocol stages. mypy is
    # configured through `files` in sim/pyproject.toml, so the call is bare.
    $uv = Find-Uv
    Push-Location (Join-Path $root "sim")
    try {
        Invoke-Stage "sim: ruff check" { & $uv run ruff check . }
        Invoke-Stage "sim: ruff format check" { & $uv run ruff format --check . }
        Invoke-Stage "sim: mypy" { & $uv run mypy }
        Invoke-Stage "sim: pytest (entire suite)" { & $uv run python tests/gate_runner.py }
    }
    finally { Pop-Location }
}

if (Test-Path (Join-Path $root "frontend\package.json")) {
    Push-Location (Join-Path $root "frontend")
    try {
        Invoke-Stage "frontend: eslint" { npm run --silent lint }
        Invoke-Stage "frontend: typecheck" { npm run --silent typecheck }
        Invoke-Stage "frontend: vitest (entire suite)" { npm run --silent test }
        if (Test-Path "playwright.config.ts") {
            Invoke-Stage "frontend: playwright" { npm run --silent test:e2e }
        }
    }
    finally { Pop-Location }
}

Write-Host ""
if ($failures.Count -gt 0) {
    Write-Host ("GATE RED: " + ($failures -join ", ")) -ForegroundColor Red
    exit 1
}
Write-Host "GATE GREEN: entire accumulated suite passed (0 skipped, 0 xfailed, 0 deselected)" `
    -ForegroundColor Green
exit 0
