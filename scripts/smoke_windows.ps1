# Smoke-test the Windows installer of the ai-cur desktop client.
#
#   pwsh scripts/smoke_windows.ps1 -Installer dist\ai-cur-desktop-setup-0.2.0.exe
#
# Installs silently (per user), asserts the files, Start Menu entry and Run key exist, starts
# the tray headless (AICUR_SMOKE=1) with a throwaway profile on a non-8000 port, asserts within
# -Timeout seconds that /health answers and the collector wrote a provider_metric_samples row,
# quits the tray (--quit) and asserts every process from the install folder is gone, starts it
# again and force-kills it and asserts the same, then uninstalls silently and asserts the
# folder, the shortcut and the Run key are gone.
#
# It also plants a live dummy process as a fake Claude Code session (~/.claude/sessions/*.json)
# BEFORE the backend starts, drives the code paths that probe those PIDs, and asserts the dummy
# is still alive: on Windows os.kill(pid, 0) is TerminateProcess, so a wrong liveness probe
# would kill the user's real sessions.
#
# Exit codes match scripts/smoke_macos.sh:
#   0 pass  2 setup error  3 /health never answered  4 no collector row
#   5 processes survived quit  6 install or uninstall left the wrong state
#   7 the backend terminated a process it only had to probe
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [int]$Port = 18765,
    [int]$Timeout = 60,
    [int]$QuitTimeout = 20,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$AppName = "ai-cur desktop client"
$App = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$Tray = Join-Path $App "aicur-tray.exe"
$Shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

function Fail([int]$Code, [string]$Message) {
    Write-Host "SMOKE FAIL ($Code): $Message"
    $logs = if ($script:ProfileDir) { Join-Path $script:ProfileDir ".usage-tracker\logs" } else { $null }
    if ($logs -and (Test-Path $logs)) {
        Get-ChildItem $logs -Filter *.log | ForEach-Object {
            Write-Host "--- $($_.Name) (tail) ---"
            Get-Content $_.FullName -Tail 40 | Write-Host
        }
    }
    Get-AppProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
    if ($script:Dummy) { Stop-Process -Id $script:Dummy.Id -Force -ErrorAction SilentlyContinue }
    # Leave the machine clean for whatever runs next (the negative control runs first).
    $uninstaller = Join-Path $App "unins000.exe"
    if (Test-Path $uninstaller) {
        Start-Process -FilePath $uninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait | Out-Null
        $until = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $until -and (Test-Path $App)) { Start-Sleep -Seconds 1 }
    }
    exit $Code
}

function Get-AppProcesses {
    Get-CimInstance Win32_Process |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($App, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
}

function Test-Listening {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-Health {
    $deadline = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$Port/health"
            if ($r.StatusCode -eq 200 -and $r.Content -match '"ok"') { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Start-Tray {
    $env:AICUR_SMOKE = "1"
    $env:USERPROFILE = $script:ProfileDir
    $env:HOME = $script:ProfileDir
    try {
        return Start-Process -FilePath $Tray -PassThru
    } finally {
        $env:USERPROFILE = $script:RealProfile
        $env:HOME = $script:RealHome
        Remove-Item Env:AICUR_SMOKE
    }
}

function Assert-AllGone([string]$How) {
    $deadline = (Get-Date).AddSeconds($QuitTimeout)
    while ((Get-Date) -lt $deadline -and (Get-AppProcesses)) { Start-Sleep -Seconds 1 }
    $left = Get-AppProcesses
    if ($left) { Fail 5 "processes survived ${How}: $(($left | ForEach-Object { "$($_.Id):$($_.ProcessName)" }) -join ' ')" }
    if (Test-Listening) { Fail 5 "port $Port still listening after $How" }
    Write-Host "${How}: tray, supervisor, api and collector all gone"
}

$script:RealProfile = $env:USERPROFILE
$script:RealHome = $env:HOME
$script:ProfileDir = $null
$script:Dummy = $null
if (-not (Test-Path $Installer)) { Fail 2 "no such installer: $Installer" }
if (Test-Listening) { Fail 2 "port $Port already has a listener" }
if (Test-Path $App) { Fail 2 "$App already exists; this test needs a clean machine" }

# ---- install ----
$p = Start-Process -FilePath $Installer -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-" -Wait -PassThru
if ($p.ExitCode -ne 0) { Fail 6 "installer exited $($p.ExitCode)" }
foreach ($path in @($Tray, $Shortcut)) {
    if (-not (Test-Path $path)) { Fail 6 "installer did not create $path" }
}
$run = (Get-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue).$AppName
if ($run -ne "`"$Tray`"") { Fail 6 "Run key value is '$run', expected '`"$Tray`"'" }
Write-Host "installed: $App (Start Menu entry and Run key present)"

# ---- start, health, collector row ----
$script:ProfileDir = Join-Path ([IO.Path]::GetTempPath()) ("aicur-smoke-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path (Join-Path $script:ProfileDir ".usage-tracker") | Out-Null
Set-Content -Path (Join-Path $script:ProfileDir ".usage-tracker\config") -Value "USAGE_TRACKER_PORT=$Port" -Encoding ascii

# A stand-in for a live Claude Code session, planted before the backend exists.
$script:Dummy = Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-Command", "Start-Sleep -Seconds 900" -WindowStyle Hidden -PassThru
$sessions = Join-Path $script:ProfileDir ".claude\sessions"
New-Item -ItemType Directory -Force -Path $sessions | Out-Null
Set-Content -Path (Join-Path $sessions "smoke-dummy.json") -Encoding ascii `
    -Value ('{"sessionId": "smoke-dummy", "pid": ' + $script:Dummy.Id + ', "cwd": "C:\\"}')
Write-Host "planted dummy Claude session pid $($script:Dummy.Id)"

$tray = Start-Tray
if (-not (Wait-Health)) { Fail 3 "GET 127.0.0.1:$Port/health did not answer within ${Timeout}s (no backend)" }
Write-Host "health answered"

$db = Join-Path $script:ProfileDir ".usage-tracker\claude_usage.db"
$rows = 0
$deadline = (Get-Date).AddSeconds($Timeout)
while ((Get-Date) -lt $deadline) {
    if (Test-Path $db) {
        $rows = & $Python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM provider_metric_samples').fetchone()[0])" $db 2>$null
        if ([int]$rows -ge 1) { break }
    }
    Start-Sleep -Seconds 1
}
if ([int]$rows -lt 1) { Fail 4 "collector wrote no provider_metric_samples row within ${Timeout}s" }
Write-Host "collector wrote $rows provider_metric_samples row(s)"

# Drive both code paths that read ~/.claude/sessions PIDs: a full work-ledger refresh (enabling
# collection starts one; it is paused by default and would skip the probe) and the session list,
# which probes every planted PID on each call. Shapes verified against a local run.
$secret = (Get-Content (Join-Path $script:ProfileDir ".usage-tracker\config") |
    Where-Object { $_ -like "USAGE_TRACKER_SECRET=*" } | Select-Object -First 1).Substring(21)
$auth = @{ Authorization = "Bearer $secret" }
$base = "http://127.0.0.1:$Port/work-ledger"
Invoke-RestMethod -Method Put -Headers $auth -ContentType "application/json" -Body '{"enabled": true}' "$base/settings" | Out-Null
Invoke-RestMethod -Method Post -Headers $auth "$base/refresh" | Out-Null
$deadline = (Get-Date).AddSeconds($Timeout)
do {
    Start-Sleep -Seconds 1
    $refresh = (Invoke-RestMethod -Headers $auth "$base/status").refresh
} while ($refresh.status -eq "running" -and (Get-Date) -lt $deadline)
if ($refresh.status -eq "running") { Fail 2 "work-ledger refresh still running after ${Timeout}s" }
$runtime = $refresh.result.claude_runtime
Write-Host "refresh: $($refresh.status); claude_runtime: $($runtime | ConvertTo-Json -Compress)"
$views = @((Invoke-RestMethod -Headers $auth "$base/sessions").sessions)
$seen = $views | Where-Object { $_.provider_session_id -eq "smoke-dummy" }
Start-Sleep -Seconds 2
if (-not (Get-Process -Id $script:Dummy.Id -ErrorAction SilentlyContinue)) {
    Fail 7 "the dummy Claude session (pid $($script:Dummy.Id)) was terminated by the backend's liveness probe"
}
# The probe must also have RUN and answered "alive", or this check proves nothing.
if (-not $seen) { Fail 2 "the refresh never reported the planted session; the probe path was not exercised" }
Write-Host "dummy Claude session pid $($script:Dummy.Id) still alive after refresh; listed as $($seen.runtime_state)"

$procs = @(Get-AppProcesses)
Write-Host "processes from the install folder: $(($procs | ForEach-Object { "$($_.Id):$($_.ProcessName)" }) -join ' ')"
if ($procs.Count -lt 4) { Fail 2 "expected tray + supervisor + api + collector, saw $($procs.Count)" }

# ---- quit cleanly ----
# aicur-tray.exe is a GUI-subsystem program: `& $Tray` would not wait for it or set
# $LASTEXITCODE, so start it and wait explicitly.
$q = Start-Process -FilePath $Tray -ArgumentList "--quit" -Wait -PassThru
if ($q.ExitCode -ne 0) { Fail 5 "aicur-tray --quit exited $($q.ExitCode) (no running tray found)" }
Assert-AllGone "quit (--quit)"

# ---- force quit ----
$tray = Start-Tray
if (-not (Wait-Health)) { Fail 3 "relaunch: /health did not answer within ${Timeout}s" }
Stop-Process -Id $tray.Id -Force
Assert-AllGone "force quit (Stop-Process -Force)"

# ---- uninstall ----
$uninstaller = Join-Path $App "unins000.exe"
if (-not (Test-Path $uninstaller)) { Fail 6 "no uninstaller at $uninstaller" }
Start-Process -FilePath $uninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait | Out-Null
# The uninstaller re-launches itself from %TEMP% and returns early; wait for its effect.
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline -and (Test-Path $App)) { Start-Sleep -Seconds 1 }
if (Test-Path $App) { Fail 6 "uninstall left $App behind: $((Get-ChildItem -Recurse $App | Select-Object -First 5 | ForEach-Object FullName) -join ', ')" }
if (Test-Path $Shortcut) { Fail 6 "uninstall left the Start Menu entry" }
if ($null -ne (Get-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue)) { Fail 6 "uninstall left the Run key value" }
Write-Host "uninstalled: folder, Start Menu entry and Run key gone"

Stop-Process -Id $script:Dummy.Id -Force -ErrorAction SilentlyContinue
Write-Host "SMOKE PASS: $Installer"
exit 0
