<#
.SYNOPSIS
    Registers the APIx daily fare-scrape as a Windows Scheduled Task.

.DESCRIPTION
    This is the reliability mechanism for unattended daily scraping on
    Windows: app/ingestion/daily_scrape_job.py (a long-running
    APScheduler process) works, but it only runs as long as *something*
    keeps its Python process alive -- close the terminal, sign out, or
    reboot, and it's gone until someone remembers to start it again.

    A Scheduled Task is what actually survives that: Windows itself owns
    the trigger, restarts the task on failure, and runs it again once the
    machine is back if it was off at the scheduled time (StartWhenAvailable).
    It invokes `python run_daily_scrape.py` directly once a day rather than
    keeping daily_scrape_job.py's BlockingScheduler running at all -- one
    process that runs, finishes, and exits is simpler to reason about
    (and to see the history of, via Get-ScheduledTaskInfo /
    `schtasks /query`) than one that has to stay alive for weeks.

    Runs under the current user account via an S4U logon (no stored
    password needed) so the task fires at 03:00 whether or not anyone is
    actually logged in interactively at that moment -- a plain Interactive
    logon type (Register-ScheduledTask's default when no -Principal is
    given) silently skips the trigger unless the user is signed in right
    then, which on a laptop that's normally logged out or locked overnight
    means the "daily" job effectively never fires on its own.

    Also sets WakeToRun so Windows wakes the machine from sleep to run it
    -- without this, a laptop that's asleep at 03:00 (the default on most
    power plans) never gets the chance to run the task at all, regardless
    of logon type -- and allows the task to start and keep running on
    battery power, since a personal laptop isn't guaranteed to be plugged
    in overnight.

.PARAMETER Hour
    Hour (0-23, local time) to run at. Default 3 (03:00), matching
    app/ingestion/daily_scrape_job.py's own default so both mechanisms
    agree if ever run side by side.

.PARAMETER Minute
    Minute (0-59) to run at. Default 0.

.EXAMPLE
    .\scripts\register_daily_scrape_task.ps1
    .\scripts\register_daily_scrape_task.ps1 -Hour 4 -Minute 30

.NOTES
    To remove: Unregister-ScheduledTask -TaskName "APIx Daily Fare Scrape" -Confirm:$false
    To inspect: Get-ScheduledTask -TaskName "APIx Daily Fare Scrape" | Get-ScheduledTaskInfo
    To run immediately (test): Start-ScheduledTask -TaskName "APIx Daily Fare Scrape"
#>
param(
    [ValidateRange(0, 23)][int]$Hour = 3,
    [ValidateRange(0, 59)][int]$Minute = 0
)

$ErrorActionPreference = "Stop"

$TaskName = "APIx Daily Fare Scrape"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $ProjectRoot "run_daily_scrape.py"
$LogDir = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $ScriptPath)) {
    throw "run_daily_scrape.py not found at $ScriptPath -- run this script from an unmoved clone of the repo."
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$VenvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    # Prefer the project venv over whatever "python" resolves to on PATH.
    # On this machine (and likely many Windows dev setups) PATH resolves to
    # the Microsoft Store / winget "PythonSoftwareFoundation.Python.3.12"
    # package under WindowsApps -- an AppX-packaged executable. AppX
    # packages generally refuse to launch outside an interactive user
    # session, which is exactly what an S4U-logon scheduled task is: the
    # very first S4U run of this task failed with a bare "Access is
    # denied" in the task log for this reason, even though the identical
    # `python run_daily_scrape.py` command worked fine run by hand in a
    # terminal. A venv's python.exe is a plain copied binary, not an AppX
    # reparse point, so it launches fine under S4U.
    $PythonExe = $VenvPython
} else {
    $PythonExe = (python -c "import sys; print(sys.executable)").Trim()
    if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
        throw "Could not resolve a python.exe on PATH. Activate the project's Python environment first."
    }
    if ($PythonExe -like "*\WindowsApps\*") {
        Write-Warning (
            "Resolved python.exe is a Microsoft Store / WindowsApps package ($PythonExe). " +
            "These typically fail to launch under this task's S4U logon (non-interactive) with " +
            "'Access is denied', even though they run fine interactively. Strongly prefer creating " +
            "a project venv first (python -m venv venv; venv\Scripts\pip install -r requirements.txt) " +
            "and re-running this script, which will then use venv\Scripts\python.exe instead."
        )
    }
}

$TaskLog = Join-Path $LogDir "scheduled_task.log"
# cmd.exe /c wraps the call purely to redirect stdout+stderr to a log file
# per run -- run_daily_scrape.py's own print()-based CLI output has
# nowhere to go once Task Scheduler launches it headlessly otherwise.
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"`"$PythonExe`" `"$ScriptPath`" >> `"$TaskLog`" 2>&1`"" `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($Hour).AddMinutes($Minute))

# S4U: runs headlessly as this user without needing a stored password --
# unlike the Interactive logon type Register-ScheduledTask defaults to,
# it doesn't require an active interactive session at trigger time.
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -DontStopOnIdleEnd `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Runs APIx's full route x AP-window Akasa Air fare scrape once daily (see app/ingestion/scheduler.py for the basket). Output logged to $TaskLog." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' -- daily at $($Hour.ToString('00')):$($Minute.ToString('00'))."
Write-Host "Run output will be appended to: $TaskLog"
Write-Host "Test it immediately with: Start-ScheduledTask -TaskName '$TaskName'"
