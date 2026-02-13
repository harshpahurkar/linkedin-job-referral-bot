Param(
    [string]$RepoName = "linkedin-job-referral-bot",
    [string]$RemoteOwner = "<your-github-username>"
)

Write-Host "Preparing to create & push repository: $RemoteOwner/$RepoName"

# Ensure sensitive files are ignored
if (-not (Test-Path .gitignore)) {
    Write-Host "No .gitignore found - aborting for safety." -ForegroundColor Red
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "git is not installed or not in PATH" -ForegroundColor Red
    exit 1
}

git init
git add .
git commit -m "Initial import (sanitized)" -q

if (Get-Command gh -ErrorAction SilentlyContinue) {
    Write-Host "Creating private repo on GitHub via gh CLI..."
    gh repo create "$RemoteOwner/$RepoName" --private --source=. --remote=origin --push
    if ($LASTEXITCODE -ne 0) { Write-Host "gh create failed" -ForegroundColor Red; exit 1 }
    Write-Host "Pushed to https://github.com/$RemoteOwner/$RepoName"
} else {
    Write-Host "gh CLI not found. To complete manually, run:" -ForegroundColor Yellow
    Write-Host "  git remote add origin https://github.com/<your-username>/$RepoName.git"
    Write-Host "  git branch -M main"
    Write-Host "  git push -u origin main"
}

Write-Host "Done. Remember to rotate any secrets if they were ever committed." -ForegroundColor Green
