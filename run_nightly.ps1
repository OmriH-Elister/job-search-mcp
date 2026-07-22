# run_nightly.ps1
# Windows wrapper for the nightly job search scan.
# Scheduled Task runs this at 10 PM daily.
# Patched 2026-07-20: Docker auto-start + readiness gate + LOUD failure notification.

$ErrorActionPreference = "Continue"
$LogFile = "$PSScriptRoot\data\nightly_wrapper.log"
$DataDir = "$PSScriptRoot\data"
# CV directory: set JOB_SEARCH_CV_DIR (User env var) or default to <repo>\cvs
$CvDir = [Environment]::GetEnvironmentVariable("JOB_SEARCH_CV_DIR", "User")
if ([string]::IsNullOrWhiteSpace($CvDir)) { $CvDir = "$PSScriptRoot\cvs" }

function Write-Log($msg) {
    Add-Content $LogFile ((Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " - " + $msg)
}

function Show-Toast($title, $body) {
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
        $template = "<toast><visual><binding template=""ToastGeneric""><text>$title</text><text>$body</text></binding></visual></toast>"
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml($template)
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Job Search Bot").Show($toast)
    } catch {
        Write-Log "Toast failed: $_"
    }
}

# Explicitly load SMTP env vars from user environment (Task Scheduler may not inherit them)
$env:JOB_SEARCH_SMTP_USER = [Environment]::GetEnvironmentVariable("JOB_SEARCH_SMTP_USER", "User")
$env:JOB_SEARCH_SMTP_PASS = [Environment]::GetEnvironmentVariable("JOB_SEARCH_SMTP_PASS", "User")

Write-Log "Starting nightly scan..."

# ---- Docker readiness gate -------------------------------------------------
$dockerReady = $false
for ($i = 0; $i -lt 40; $i++) {
    & docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
    if ($i -eq 0) {
        Write-Log "Docker not responding. Launching Docker Desktop..."
        Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe" -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 15
}

if (-not $dockerReady) {
    Write-Log "ABORT: Docker engine never became ready after ~10 minutes."
    Show-Toast "Job Search: SCAN FAILED" "Docker engine did not start. Nightly scan did not run."
    exit 1
}
Write-Log "Docker engine ready."

# ---- Image presence check --------------------------------------------------
$img = & docker images -q job-search-mcp:latest 2>&1
if ([string]::IsNullOrWhiteSpace($img)) {
    Write-Log "ABORT: image job-search-mcp:latest not found. Rebuild with: docker build -t job-search-mcp ."
    Show-Toast "Job Search: SCAN FAILED" "Docker image job-search-mcp:latest is missing. Rebuild required."
    exit 1
}

try {
    docker run --rm `
        -v "${DataDir}:/data" `
        -v "${CvDir}:/cvs:ro" `
        -v "${PSScriptRoot}\config.json:/app/config.json" `
        -v "${PSScriptRoot}\run_scan.py:/app/run_scan.py" `
        -e "BROWSER_HEADLESS=true" `
        -e "SMTP_HOST=smtp.gmail.com" `
        -e "SMTP_PORT=587" `
        -e "SMTP_USER=$env:JOB_SEARCH_SMTP_USER" `
        -e "SMTP_PASS=$env:JOB_SEARCH_SMTP_PASS" `
        -e "NOTIFY_TO=$env:JOB_SEARCH_SMTP_USER" `
        --entrypoint python `
        job-search-mcp:latest `
        run_scan.py

    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        Write-Log "Scan completed successfully."

        $summaryFile = "$DataDir\last_scan_summary.json"
        if (Test-Path $summaryFile) {
            $summary = Get-Content $summaryFile -Raw | ConvertFrom-Json
            $newMatches  = $summary.new_matches_found
            $totalPending = $summary.total_pending_review

            if ($newMatches -gt 0) {
                $toastTitle = "Job Search: $newMatches new match(es)!"
                $toastBody  = "$totalPending total pending review. Open Claude to review letters."
            } elseif ($totalPending -gt 0) {
                $toastTitle = "Job Search: $totalPending pending"
                $toastBody  = "Open Claude and say 'Show me pending job applications'."
            } else {
                $toastTitle = "Job Search: No new matches"
                $toastBody  = "All caught up. Will scan again tomorrow."
            }
            Show-Toast $toastTitle $toastBody
            Write-Log "Toast notification shown: $toastTitle"
        } else {
            Write-Log "WARNING: scan exited 0 but no last_scan_summary.json was written."
            Show-Toast "Job Search: incomplete" "Scan finished but produced no summary file."
        }
    } else {
        Write-Log "Scan failed with exit code $exitCode"
        Show-Toast "Job Search: SCAN FAILED" "run_scan.py exited with code $exitCode. Check nightly_wrapper.log."
        exit $exitCode
    }
} catch {
    Write-Log "Error: $_"
    Show-Toast "Job Search: SCAN ERROR" "Wrapper threw an exception. Check nightly_wrapper.log."
    exit 1
}