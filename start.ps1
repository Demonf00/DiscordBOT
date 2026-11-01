param(
  [string]$WTAgentRoot = "D:\Github\wtagent-selfdev",
  [string]$WorkspaceRoot = "D:\AgentWorkspace"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$entry = Join-Path $WTAgentRoot "src\supervisor\service-main.js"
if (!(Test-Path $entry)) { throw "WTAgent source not found at $WTAgentRoot. Pass -WTAgentRoot path." }
if (!(Get-Command node -ErrorAction SilentlyContinue)) { throw "Node.js is required on PATH." }
if (Get-Command py -ErrorAction SilentlyContinue) {
  $pythonExe = "py"
  $pythonPrefix = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $pythonExe = "python"
  $pythonPrefix = @()
} else { throw "Python 3 is required. Install Python and re-run." }

$localDir = Join-Path $PSScriptRoot ".local"
$envPath = Join-Path $PSScriptRoot ".env"
$supervisorConfig = Join-Path $localDir "supervisor-config.json"
New-Item -ItemType Directory -Force -Path $localDir, $WorkspaceRoot | Out-Null

if (!(Test-Path $envPath)) {
  Write-Host ""
  Write-Host "First-time Discord setup. Secrets are stored ONLY in this git-ignored .env file." -ForegroundColor Yellow
  Write-Host "Enable Message Content Intent in the Discord Developer Portal first." -ForegroundColor Yellow
  $botSecret = Read-Host "Discord Bot Token" -AsSecureString
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($botSecret)
  try { $botToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
  $owner = (Read-Host "Your Discord User ID (Developer Mode: Copy User ID)").Trim()
  $channel = (Read-Host "Allowed Discord Channel ID (Developer Mode: Copy Channel ID)").Trim()
  if ($owner -notmatch '^[0-9]{15,22}$' -or $channel -notmatch '^[0-9]{15,22}$') {
    throw "User ID and Channel ID must be Discord numeric IDs."
  }
  if ($botToken -notmatch '^[A-Za-z0-9_.-]{20,}$') {
    throw "Discord Bot Token does not match the expected format."
  }
  $randomBytes = New-Object byte[] 32
  $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
  try { $generator.GetBytes($randomBytes) } finally { $generator.Dispose() }
  $supervisorToken = ([BitConverter]::ToString($randomBytes)).Replace("-", "").ToLowerInvariant()
  $lines = @(
    "DISCORD_BOT_TOKEN=$botToken"
    "DISCORD_ALLOWED_USERS=$owner"
    "DISCORD_ALLOWED_CHANNELS=$channel"
    "WTAGENT_SUPERVISOR_URL=http://127.0.0.1:8765"
    "WTAGENT_SUPERVISOR_TOKEN=$supervisorToken"
    "WTAGENT_WORKSPACE_ID=discord"
  )
  [IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
  $botToken = $null
  Write-Host "Configuration saved locally: $envPath"
}
$config = @{}
foreach ($line in Get-Content $envPath) {
  if ($line -match '^([A-Z_]+)=(.*)$') { $config[$Matches[1]] = $Matches[2] }
}
if (!$config["WTAGENT_SUPERVISOR_TOKEN"] -or !$config["DISCORD_BOT_TOKEN"]) {
  throw "Invalid .env. Remove/repair it locally then rerun."
}
$settings = @{
  port = 8765
  workspaces = @(@{ id = "discord"; root = (Resolve-Path $WorkspaceRoot).Path })
}
$json = $settings | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($supervisorConfig, $json, (New-Object Text.UTF8Encoding($false)))

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (!(Test-Path $venvPython)) {
  & $pythonExe @pythonPrefix -m venv (Join-Path $PSScriptRoot ".venv")
  if ($LASTEXITCODE -ne 0) { throw "Failed to create Python virtual environment." }
}
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }

$env:WTAGENT_SUPERVISOR_TOKEN = $config["WTAGENT_SUPERVISOR_TOKEN"]
$node = (Get-Command node).Source
$stdoutPath = Join-Path $localDir "supervisor.stdout.log"
$stderrPath = Join-Path $localDir "supervisor.stderr.log"
$service = $null
try {
  $quotedEntry = '"' + $entry + '"'
  $quotedConfig = '"' + $supervisorConfig + '"'
  $service = Start-Process -FilePath $node -ArgumentList @($quotedEntry, "--config", $quotedConfig) -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru -NoNewWindow
  $headers = @{ Authorization = "Bearer $($config['WTAGENT_SUPERVISOR_TOKEN'])" }
  $healthy = $false
  for ($i = 0; $i -lt 25; $i++) {
    if ($service.HasExited) { break }
    try {
      $null = Invoke-RestMethod -Uri "http://127.0.0.1:8765/v1/tasks" -Headers $headers -TimeoutSec 2
      $healthy = $true
      break
    } catch { Start-Sleep -Milliseconds 500 }
  }
  if (!$healthy) {
    Write-Host "Supervisor could not start. Its recent errors:" -ForegroundColor Red
    if (Test-Path $stderrPath) { Get-Content $stderrPath -Tail 25 }
    throw "Supervisor health check failed."
  }
  Write-Host ""
  Write-Host "WTAgent Supervisor ready on 127.0.0.1:8765." -ForegroundColor Green
  Write-Host "Starting Discord Bot. Type a normal message in your allowed channel." -ForegroundColor Green
  Write-Host "Ctrl+C stops the Discord Bot and the Supervisor started by this script."
  & $venvPython (Join-Path $PSScriptRoot "jarvis.py")
  if ($LASTEXITCODE -ne 0) { throw "Discord bot exited with code $LASTEXITCODE" }
} finally {
  if ($service -and !$service.HasExited) {
    Stop-Process -Id $service.Id -ErrorAction SilentlyContinue
  }
}
