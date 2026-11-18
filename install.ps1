# Forge installer for Windows (NVIDIA GPU recommended). One command, fully set up:
#
#   irm https://raw.githubusercontent.com/eshangulati1234-a11y/forge-agent/main/install.ps1 | iex
#
# Idempotent: safe to re-run to update or repair.
#   $env:FORGE_NO_PULL=1      skip model downloads
#   $env:FORGE_NO_STARTUP=1   skip the start-at-login task
$ErrorActionPreference = "Stop"
$RepoUrl = if ($env:FORGE_REPO_URL) { $env:FORGE_REPO_URL } else { "https://github.com/eshangulati1234-a11y/forge-agent.git" }
$AppDir  = if ($env:FORGE_APP_DIR)  { $env:FORGE_APP_DIR }  else { Join-Path $env:USERPROFILE ".forge-app" }

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User") }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say "Installing uv"
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  Refresh-Path
  $env:Path += ";$env:USERPROFILE\.local\bin"
}

$Src = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml")) -and (Select-String -Path (Join-Path $PSScriptRoot "pyproject.toml") -Pattern '^name = "forge"' -Quiet)) { $Src = $PSScriptRoot }
if (-not $Src) {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Say "Installing git"; winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements; Refresh-Path }
  if (Test-Path (Join-Path $AppDir ".git")) { Say "Updating Forge in $AppDir"; git -C $AppDir pull --ff-only }
  else { Say "Downloading Forge to $AppDir"; git clone --depth 1 $RepoUrl $AppDir }
  $Src = $AppDir
}
Set-Location $Src

Say "Installing Python dependencies"
uv sync --python 3.12

# put `forge` on the user PATH
$Bin = Join-Path $Src ".venv\Scripts"
$UserPath = [Environment]::GetEnvironmentVariable("Path","User")
if ($UserPath -notlike "*$Bin*") { [Environment]::SetEnvironmentVariable("Path", "$UserPath;$Bin", "User"); Say "Added $Bin to your PATH" }

$ArgsList = @("setup","--yes","--install-ollama")
if ($env:FORGE_NO_PULL -eq "1") { $ArgsList += "--no-pull" }
if ($env:FORGE_NO_STARTUP -eq "1") { $ArgsList += "--no-startup" } else { $ArgsList += "--startup" }
Say "Running Forge setup (downloads models; this can take a while the first time)"
& (Join-Path $Bin "forge.exe") @ArgsList

Write-Host ""
Say "Done. Open a new terminal and start with:  forge"
