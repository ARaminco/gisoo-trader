<#
    Gisoo Trader - one-command startup for Windows.

    Usage:
      .\start.ps1                  build if needed, then start everything
      .\start.ps1 -Rebuild         force a rebuild of the images
      .\start.ps1 -Logs            follow the logs after starting
      .\start.ps1 -Stop            stop everything
      .\start.ps1 -Status          show service status
      .\start.ps1 -Backup          take a database backup into .\backups now
      .\start.ps1 -Restore FILE    replace the database with .\backups\FILE (stops the workers first)
      .\start.ps1 -Autostart       also register a scheduled task so it starts at login,
                                   and print the host settings you must enable by hand
      -NonInteractive              never prompt (the scheduled task passes this)
#>
[CmdletBinding(PositionalBinding=$false)]
param(
    [switch]$Rebuild,
    [switch]$Logs,
    [switch]$Stop,
    [switch]$Autostart,
    [switch]$Status,
    [switch]$Backup,
    [string]$Restore,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "  + $message" -ForegroundColor Green }
function Write-Warn2($message){ Write-Host "  ! $message" -ForegroundColor Yellow }
function Write-Err($message)  { Write-Host "  x $message" -ForegroundColor Red }

function New-Hex([int]$bytes) {
    # CSPRNG - Get-Random is not one on PowerShell 5.1.
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    ($buf | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Test-Docker {
    # Redirection stays inside cmd so PowerShell 5.1 never turns stderr into an exception.
    cmd /c "docker version >nul 2>&1"
    return ($LASTEXITCODE -eq 0)
}

function Test-Clock {
    # Binance rejects signed requests whose timestamp is off by more than a few
    # seconds (error -1021). Compare the local clock with the exchange.
    try {
        $before = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $response = Invoke-WebRequest -Uri 'https://api.binance.com/api/v3/time' -TimeoutSec 5 -UseBasicParsing
        $after = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $server = [int64]($response.Content | ConvertFrom-Json).serverTime
        $local = [int64](($before + $after) / 2)
        $offset = $local - $server
        if ([Math]::Abs($offset) -gt 2000) {
            Write-Warn2 ("Local clock is {0:N1} s {1} Binance time. Signed orders will fail when this drifts." -f ([Math]::Abs($offset) / 1000), $(if ($offset -gt 0) { 'AHEAD of' } else { 'BEHIND' }))
            Write-Host '     Enable time sync once, in an elevated PowerShell:' -ForegroundColor Yellow
            Write-Host '       Set-Service W32Time -StartupType Automatic; Start-Service W32Time' -ForegroundColor DarkGray
            Write-Host '       w32tm /config /manualpeerlist:"time.windows.com,0x9 time.cloudflare.com,0x9" /syncfromflags:manual /update' -ForegroundColor DarkGray
            Write-Host '       w32tm /resync /force; w32tm /query /status' -ForegroundColor DarkGray
        } else {
            Write-Ok ("Clock is within {0:N1} s of Binance" -f ([Math]::Abs($offset) / 1000))
        }
    } catch {
        Write-Warn2 "Could not compare the clock with Binance ($($_.Exception.Message))"
    }
}

# ---------------------------------------------------------------- docker

Write-Step 'Waiting for Docker'
$deadline = (Get-Date).AddMinutes(5)
$announced = $false
while (-not (Test-Docker)) {
    if ((Get-Date) -gt $deadline) {
        Write-Err 'Docker Desktop did not start within 5 minutes.'
        exit 1
    }
    if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (Test-Path $exe) {
            if (-not $announced) { Write-Warn2 'Docker Desktop is not running - starting it' }
            Start-Process $exe | Out-Null
        } elseif (-not $announced) {
            Write-Warn2 'Docker Desktop is not running and was not found in Program Files. Start it by hand.'
        }
    } elseif (-not $announced) {
        Write-Host '    Docker Desktop is starting...' -ForegroundColor DarkGray
    }
    $announced = $true
    Start-Sleep -Seconds 10
}
Write-Ok 'Docker is running'

if ($Stop) {
    Write-Step 'Stopping all services'
    docker compose down
    Write-Ok 'Stopped'
    exit 0
}

if ($Status) {
    docker compose ps
    exit 0
}

if ($Backup) {
    Write-Step 'Backing up the database into .\backups'
    New-Item -ItemType Directory -Force -Path 'backups' | Out-Null
    docker compose run --rm --no-deps backup once
    if ($LASTEXITCODE -ne 0) { Write-Err 'Backup failed'; exit 1 }
    Get-ChildItem 'backups' | Sort-Object LastWriteTime -Descending | Select-Object -First 4 Name, Length, LastWriteTime | Format-Table -AutoSize
    exit 0
}

if ($Restore) {
    $name = Split-Path -Leaf $Restore
    if (-not (Test-Path (Join-Path 'backups' $name))) { Write-Err "backups\$name not found"; exit 2 }
    Write-Warn2 "This REPLACES the live database with backups\$name (a safety dump is taken first)."
    if (-not $NonInteractive) {
        $answer = Read-Host 'Type RESTORE to continue'
        if ($answer -ne 'RESTORE') { Write-Host 'aborted'; exit 1 }
    }
    Write-Step 'Stopping the workers'
    docker compose stop api engine marketdata scheduler
    Write-Step 'Restoring'
    docker compose run --rm --no-deps backup restore $name
    if ($LASTEXITCODE -ne 0) { Write-Err 'Restore failed - the database is unchanged. Start the workers with .\start.ps1'; exit 1 }
    Write-Step 'Starting the stack again (migration runs first)'
    docker compose up -d
    docker compose ps
    exit 0
}

# ---------------------------------------------------------------- .env

if (-not (Test-Path '.env')) {
    Write-Step 'Creating .env from the template'
    $content = Get-Content '.env.example' -Raw
    $content = $content -replace 'JWT_SECRET=change-me-to-a-long-random-string', "JWT_SECRET=$(New-Hex 32)"
    $content = $content -replace 'POSTGRES_PASSWORD=change-this-db-password', "POSTGRES_PASSWORD=$(New-Hex 12)"
    $panelPassword = New-Hex 9
    $content = $content -replace '(?m)^PANEL_PASSWORD=change-me-now', "PANEL_PASSWORD=$panelPassword"
    # No BOM: `source .env` on a Linux VPS would choke on it.
    [IO.File]::WriteAllText((Join-Path $root '.env'), $content, (New-Object System.Text.UTF8Encoding $false))
    # Only this account (and SYSTEM) may read the exchange secret.
    try { icacls .env /inheritance:r /grant:r "$($env:USERNAME):(R,W)" "SYSTEM:(F)" | Out-Null } catch { }

    Write-Ok '.env created with generated secrets'
    Write-Warn2 "Panel login: admin / $panelPassword  (change it in .env or set PANEL_PASSWORD_HASH)"
    Write-Warn2 'Now open .env and fill in:'
    Write-Host '     EXCHANGE + EXCHANGE_API_KEY / EXCHANGE_API_SECRET (not needed in paper mode)' -ForegroundColor Yellow
    Write-Host '     GISOO_API_KEY (optional, for the AI layer - https://gisoo.pro)' -ForegroundColor Yellow
    Write-Host ''
    if ($NonInteractive) {
        Write-Err 'Running non-interactively: fill in .env and run .\start.ps1 again.'
        exit 1
    }
    $answer = Read-Host 'Open .env now? (y/n)'
    if ($answer -eq 'y') { notepad.exe '.env'; Read-Host 'Press Enter when you have saved it' }
}

# warn about defaults that must not survive into live trading
$envText = Get-Content '.env' -Raw
if ($envText -match '(?m)^PANEL_PASSWORD=change-me-now' -and $envText -match "(?m)^PANEL_PASSWORD_HASH=''") {
    Write-Warn2 'PANEL_PASSWORD is still the default - the panel refuses every login until you change it.'
}
if ($envText -match '(?m)^TRADING_MODE=live' -and $envText -notmatch '(?m)^(EXCHANGE|BINANCE)_API_KEY=\S') {
    Write-Warn2 'TRADING_MODE is live but no exchange API key is set. The engine will not place orders.'
}

$port = 8090
if ($envText -match '(?m)^PANEL_PORT=(\d+)') { $port = $Matches[1] }
$bind = '127.0.0.1'
if ($envText -match '(?m)^PANEL_BIND=(\S+)') { $bind = $Matches[1] }
$site = ':80'
if ($envText -match '(?m)^PUBLIC_SITE_ADDRESS=(\S+)') { $site = $Matches[1] }
$composeFile = ''
if ($envText -match '(?m)^COMPOSE_FILE=(\S+)') { $composeFile = $Matches[1] }
$vpsMode = ($site -notmatch '^:\d+$')

if ($vpsMode -and -not $composeFile) {
    Write-Warn2 "PUBLIC_SITE_ADDRESS=$site but COMPOSE_FILE is not set: add COMPOSE_FILE=docker-compose.yml:docker-compose.vps.yml to .env so ports 80/443 are published."
}
if (-not $vpsMode -and $bind -eq '0.0.0.0') {
    Write-Host "    Panel is reachable from your LAN on port $port (PANEL_BIND=0.0.0.0). Set PANEL_BIND=127.0.0.1 in .env to keep it local." -ForegroundColor DarkGray
}

$exchange = 'binance'
if ($envText -match '(?m)^EXCHANGE=(\S+)') { $exchange = $Matches[1] }
if ($exchange -eq 'binance') {
    Write-Step 'Checking the clock against Binance'
    Test-Clock
}

# ---------------------------------------------------------------- build & run

New-Item -ItemType Directory -Force -Path 'backups' | Out-Null

if ($Rebuild) {
    Write-Step 'Rebuilding images (this takes a few minutes the first time)'
    docker compose build --no-cache
} else {
    Write-Step 'Building images if needed'
    docker compose build
}
if ($LASTEXITCODE -ne 0) { Write-Err 'Build failed'; exit 1 }

Write-Step 'Starting services (the schema migration runs first)'
docker compose up -d --remove-orphans
if ($LASTEXITCODE -ne 0) { Write-Err 'Startup failed'; exit 1 }

Write-Step 'Waiting for the API'
$ready = $false
foreach ($i in 1..60) {
    Start-Sleep -Seconds 2
    $id = (docker compose ps -q api 2>$null | Select-Object -First 1)
    if ($id) {
        $health = docker inspect -f '{{.State.Health.Status}}' $id 2>$null
        if ($health -eq 'healthy') { $ready = $true; break }
    }
    if ($i % 5 -eq 0) { Write-Host "    still waiting... ($($i*2)s)" -ForegroundColor DarkGray }
}

Write-Host ''
if ($ready) {
    if ($vpsMode) {
        Write-Ok "Panel is up:  https://$site"
    } else {
        Write-Ok "Panel is up:  http://localhost:$port"
    }
    Write-Host ''
    Write-Host '  Install it as an app:' -ForegroundColor Gray
    Write-Host '    Chrome/Edge on this PC: open the URL, then the install icon in the address bar' -ForegroundColor DarkGray
    Write-Host '    Phone: needs HTTPS, i.e. the VPS setup (PUBLIC_SITE_ADDRESS). Over plain http on the LAN' -ForegroundColor DarkGray
    Write-Host '           the panel works in the browser, but "Add to Home Screen" is only a bookmark.' -ForegroundColor DarkGray
} else {
    Write-Warn2 'The API did not become healthy in time. Check the logs:'
    Write-Host '    docker compose logs -f migrate api' -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- autostart

if ($Autostart) {
    Write-Step 'Registering the startup task'
    $taskName = 'Gisoo Trader'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\start.ps1`" -NonInteractive" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $trigger.Delay = 'PT2M'     # let Docker Desktop begin its own start first
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 2) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew

    try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop } catch { }
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description 'Start Gisoo Trader at login (waits up to 5 min for Docker)' | Out-Null
    Write-Ok "Scheduled task '$taskName' registered (runs 2 min after login, retries 10x every 2 min)"

    Write-Host ''
    Write-Host '  The task can only do its job if you enable these on the host (once, by hand):' -ForegroundColor Yellow
    Write-Host '   1. Docker Desktop -> Settings -> General -> "Start Docker Desktop when you sign in"' -ForegroundColor Yellow
    Write-Host '   2. Time sync (elevated PowerShell) - Binance rejects orders from a drifting clock:' -ForegroundColor Yellow
    Write-Host '        Set-Service W32Time -StartupType Automatic; Start-Service W32Time' -ForegroundColor DarkGray
    Write-Host '        w32tm /config /manualpeerlist:"time.windows.com,0x9 time.cloudflare.com,0x9" /syncfromflags:manual /update' -ForegroundColor DarkGray
    Write-Host '        w32tm /resync /force; w32tm /query /status' -ForegroundColor DarkGray
    Write-Host '   3. Automatic sign-in after a reboot (Docker Desktop needs a user session):' -ForegroundColor Yellow
    Write-Host '        Settings -> Accounts -> Sign-in options -> turn OFF "Use my sign-in info to ... after an update",' -ForegroundColor DarkGray
    Write-Host '        then `netplwiz` -> untick "Users must enter a user name and password" (or use Sysinternals Autologon)' -ForegroundColor DarkGray
    Write-Host '   4. Never sleep: Settings -> System -> Power -> Sleep = Never (also on battery), and in an elevated prompt:' -ForegroundColor Yellow
    Write-Host '        powercfg /change standby-timeout-ac 0; powercfg /change standby-timeout-dc 0; powercfg /hibernate off' -ForegroundColor DarkGray
    Write-Host '   5. Windows Update -> Advanced options -> active hours, so reboots happen when you expect them' -ForegroundColor Yellow
    Write-Host '  Docker Desktop cannot run as a Windows service; truly unattended operation is the VPS (docs/architecture.md).' -ForegroundColor DarkGray
}

Write-Host ''
docker compose ps

if ($Logs) {
    Write-Host ''
    Write-Step 'Following logs (Ctrl+C to stop - the services keep running)'
    docker compose logs -f --tail 50
}
