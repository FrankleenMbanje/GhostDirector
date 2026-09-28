# GhostDirector daily scheduler registration (Phase 10).
#
# Registers a Windows Task Scheduler entry that runs the Fame Files daily
# production at 08:00 local time (Africa/Harare), every day. Restart-safe:
# the run lock + database idempotency in storage/scheduler.py prevent
# duplicate production; a crash leaves state that the next run reclaims.
#
# Run from an elevated PowerShell, from the repository root:
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1
#
# Remove with:
#   Unregister-ScheduledTask -TaskName "GhostDirector Daily Production" -Confirm:$false

$taskName = "GhostDirector Daily Production"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python   = Join-Path $repoRoot "venv\Scripts\python.exe"
$main     = Join-Path $repoRoot "main.py"
$workdir  = $repoRoot

if (-not (Test-Path $python)) {
    Write-Error "venv python not found at $python - create the venv first."
    exit 1
}

$action    = New-ScheduledTaskAction -Execute (Join-Path $repoRoot "scripts\daily_task.cmd") -WorkingDirectory $workdir
$trigger   = New-ScheduledTaskTrigger -Daily -At 08:00
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 `
               -RestartInterval (New-TimeSpan -Minutes 10) `
               -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
               -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
               -WakeToRun
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

$desc = "Fame Files daily production at 08:00 (Africa/Harare): short first, then the 8-min+ long-form. Idempotent: storage/scheduler.py refuses duplicate runs. Runs whether the user is logged on or not (S4U) and wakes the PC from sleep."

# FIX-068: bare $env:USERNAME makes Register-ScheduledTask fail with
# "The parameter is incorrect (20,8):UserId"; the full DOMAIN\user form is
# required, and S4U (run-while-logged-off) registration needs an ELEVATED
# shell: non-elevated shells get "Access is denied" and fall back to an
# interactive-logon task (runs only while the user is logged on).
# NOTE: NO em-dashes anywhere in this file (FIX-067: PS 5.1 reads it as
# ANSI and a smart-quote byte terminates strings early).
try {
    Register-ScheduledTask -TaskName $taskName `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description $desc -Force -ErrorAction Stop | Out-Null
    Write-Host "Mode: S4U - runs whether the user is logged on or not."
} catch {
    Write-Warning "S4U principal rejected ($($_.Exception.Message)); registering as interactive-logon task instead."
    Register-ScheduledTask -TaskName $taskName `
        -Action $action -Trigger $trigger -Settings $settings `
        -Description $desc -Force | Out-Null
    Write-Host "Mode: interactive only - the user must be logged on at 08:00."
}

Write-Host ""
Write-Host "Registered '$taskName' - daily at 08:00, StartWhenAvailable, WakeToRun."
Write-Host "Test now:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Status:    Get-ScheduledTaskInfo -TaskName '$taskName'"
Write-Host "Database:  set DATABASE_URL before the task fires (see .env.example)."
