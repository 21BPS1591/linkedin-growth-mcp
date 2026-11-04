# Registers a Windows task that starts the LinkedIn growth MCP server whenever you sign in, with NO console window.
# Run once from the project folder:  powershell -ExecutionPolicy Bypass -File ./install-autostart.ps1
# Re-running replaces the task. Remove it with:  Unregister-ScheduledTask -TaskName LinkedInGrowthMCP -Confirm:$false
# The server logs to data\server.log (pythonw.exe has no console).
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found at $pythonw" }
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m linkedin_growth_mcp --transport http --host 127.0.0.1 --port 8000" -WorkingDirectory $project
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Watchdog: every 5 minutes try to start it. MultipleInstances=IgnoreNew makes this a no-op while it is running,
# so if the server is ever stopped (Ctrl+C, crash, sleep kill) it comes back within 5 minutes.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$trigger = @($logon, $watchdog)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "LinkedInGrowthMCP" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Starts the LinkedIn growth MCP server (autopilot) when you sign in, hidden" -Force | Select-Object TaskName, State
