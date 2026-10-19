param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskName = 'The Fame Files - Local Daily 14'
$repoRoot = Split-Path -Parent $PSScriptRoot
if ($Remove) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue; exit 0 }
$python = Join-Path $repoRoot 'venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Set up the local Python runtime first.' }
if ([TimeZoneInfo]::Local.GetUtcOffset((Get-Date)).TotalHours -ne 2) { throw 'Set the intended South Africa timezone before registering 14:00.' }
$backupDirectory = Join-Path $repoRoot 'output\local-studio\scheduler-backup'
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
$legacy = Get-ScheduledTask -TaskName 'GhostDirector Daily Production' -ErrorAction SilentlyContinue
if ($legacy) {
    Export-ScheduledTask -TaskName $legacy.TaskName | Set-Content -LiteralPath (Join-Path $backupDirectory 'legacy-task.xml')
    Disable-ScheduledTask -TaskName $legacy.TaskName | Out-Null
}
$action = New-ScheduledTaskAction -Execute $python -Argument '-m local_studio --daily' -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At '14:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$mode = 'S4U'
$description = 'Local Fame Files starts at 14:00 South Africa time. Minimum five-minute review video, varied clean licensed sources and three thumbnails. No paid AI APIs or uploads. Requires PC power, internet, storage and installed local models.'
$registeredOk = $false
try {
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $description -Force -ErrorAction Stop | Out-Null
    $registeredOk = $true
} catch {
    $mode = 'Interactive - user must be logged on'
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description ($description + ' Interactive logon required.') -Force -ErrorAction Stop | Out-Null
        $registeredOk = $true
    } catch { $registeredOk = $false }
}
if (-not $registeredOk) {
    $startup = [Environment]::GetFolderPath('Startup')
    $shortcutPath = Join-Path $startup 'The Fame Files Local Scheduler.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $python
    $shortcut.Arguments = '-m local_studio --schedule-loop'
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.Description = 'The Fame Files daily local production at 2pm South Africa time. Requires the PC awake and signed in.'
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $shortcutPath)) { throw 'User-level scheduler installation failed.' }
    $process = Start-Process -FilePath $python -ArgumentList '-m local_studio --schedule-loop' -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru
    [ordered]@{mode='per-user sign-in scheduler';daily_start='14:00';timezone='Africa/Johannesburg';startup_shortcut=$shortcutPath;process_id=$process.Id;requires_awake_signed_in=$true;uploads_enabled=$false;task_scheduler_registered=$false} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $repoRoot 'output\local-studio\schedule.json')
    Write-Output 'Installed the per-user 14:00 scheduler. Windows Task Scheduler denied registration. PC must be awake and signed in.'
    exit 0
}
$registered = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[ordered]@{ task_name=$taskName; mode=$mode; daily_start='14:00'; timezone='Africa/Johannesburg'; trigger=$registered.Triggers.StartBoundary; next_run=$info.NextRunTime.ToString('o'); action=$registered.Actions.Execute; arguments=$registered.Actions.Arguments; uploads_enabled=$false } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $repoRoot 'output\local-studio\schedule.json')
Write-Output "Registered $taskName. Mode: $mode. Next start: $($info.NextRunTime)."
