# Requires PowerShell 5.1+ and Git
# Creates GitHub repo and pushes initial commit
param(
    [string]$GitHubUser = "",
    [ValidateSet("public", "private")]
    [string]$Visibility = "public"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git not found. Install with: winget install Git.Git" -ForegroundColor Red
    exit 1
}

if (-not $GitHubUser) {
    $GitHubUser = Read-Host "Enter your GitHub username"
}

if (-not (Test-Path ".git")) {
    git init
    git branch -M main
}

git add -A
git commit -m "Initial commit: trends video pipeline with dashboard" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Nothing to commit or commit failed." -ForegroundColor Yellow
}

$remoteUrl = "https://github.com/$GitHubUser/trends-video-pipeline.git"

if (-not (git remote get-url origin 2>$null)) {
    git remote add origin $remoteUrl
}

Write-Host ""
Write-Host "Create the repo on GitHub first:" -ForegroundColor Cyan
Write-Host "  https://github.com/new?name=trends-video-pipeline"
Write-Host ""
Write-Host "Then push:" -ForegroundColor Cyan
Write-Host "  git push -u origin main"
Write-Host ""
Write-Host "Or if gh CLI is installed:" -ForegroundColor Cyan
Write-Host "  gh repo create trends-video-pipeline --$Visibility --source=. --push"
