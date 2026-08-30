# Sync LLM keys/config from ../comm-assistant/.env into this project's .env
# Usage: .\scripts\sync-llm-env-from-comm-assistant.ps1
# Never prints secret values.

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source } else { $python = $null }
}
if (-not $python) {
    Write-Host "Python not found. Run scripts\setup-windows.ps1 first." -ForegroundColor Red
    exit 1
}

& $python (Join-Path $PSScriptRoot "sync_llm_env_from_comm_assistant.py")
exit $LASTEXITCODE
