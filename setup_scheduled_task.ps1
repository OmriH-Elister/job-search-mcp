# setup_scheduled_task.ps1
# Run this ONCE as Administrator to create the nightly scan task.
#
# Before running, set your Gmail app password as environment variables:
#   [System.Environment]::SetEnvironmentVariable("JOB_SEARCH_SMTP_USER", "your@gmail.com", "User")
#   [System.Environment]::SetEnvironmentVariable("JOB_SEARCH_SMTP_PASS", "your-app-password", "User")
#
# To generate an app password:
#   1. Go to https://myaccount.google.com/apppasswords
#   2. Select "Mail" and generate
#   3. Use that 16-char password above

$TaskName = "JobSearchNightlyScan"
$ScriptPath = Join-Path $PSScriptRoot "run_nightly.ps1"
$Description = "Runs the job search MCP nightly scan at 10 PM, finds matching jobs, drafts cover letters, and sends a notification."

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task '$TaskName'"
}

# Trigger: Daily at 10:00 PM
$Trigger = New-ScheduledTaskTrigger -Daily -At "10:00PM"

# Action: Run PowerShell with the wrapper script
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
    -WorkingDirectory $PSScriptRoot

# Settings
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# Principal: run as current user, no elevation needed
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Register the task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal `
    -Description $Description

Write-Host ""
Write-Host "✅ Scheduled task '$TaskName' created successfully!"
Write-Host "   Schedule: Daily at 10:00 PM"
Write-Host "   Script:   $ScriptPath"
Write-Host ""
Write-Host "To test it now, run:"
Write-Host "   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To check status:"
Write-Host "   Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host "To remove it:"
Write-Host "   Unregister-ScheduledTask -TaskName '$TaskName'"
