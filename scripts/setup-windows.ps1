# Requires PowerShell 5.1+
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

Write-Host "=== News Shorts Pipeline - Windows Setup ===" -ForegroundColor Cyan

$python = $null
$candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)
foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notmatch "WindowsApps") {
            $python = $cmd.Source
            break
        }
    }
}
if (-not $python) {
    Write-Host "Python not found. Install with: winget install Python.Python.3.12" -ForegroundColor Red
    exit 1
}
Write-Host "Using Python: $python"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "FFmpeg not found. Install with: winget install Gyan.FFmpeg" -ForegroundColor Yellow
} else {
    Write-Host "FFmpeg: OK"
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & $python -m venv .venv
}

Write-Host "Installing dependencies..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example - fill in Groq / YouTube credentials." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path output | Out-Null

$trendsSecrets = Join-Path (Resolve-Path ..).Path "trends-video-pipeline\secrets"
if (-not (Test-Path "secrets")) {
    if (Test-Path $trendsSecrets) {
        cmd /c mklink /J secrets "$trendsSecrets" | Out-Null
        Write-Host "Linked secrets/ to trends-video-pipeline/secrets (same YouTube channel)" -ForegroundColor Green
    } else {
        New-Item -ItemType Directory -Force -Path secrets | Out-Null
        Write-Host "Created empty secrets/ - copy client_secrets.json + token.json from trends" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Start: .\scripts\run.ps1"
Write-Host "  2. Open: http://127.0.0.1:8081"
Write-Host "  3. Generate Shorts per section (or use --mock for a smoke test)"
