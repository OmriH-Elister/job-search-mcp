"""
Email notification for nightly scan results.
Uses Gmail SMTP with an App Password.

Setup:
  1. Go to https://myaccount.google.com/apppasswords
  2. Generate an app password for "Mail"
  3. Set env vars SMTP_USER and SMTP_PASS in docker run or .env
"""

import logging
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
NOTIFY_TO = os.getenv("NOTIFY_TO", SMTP_USER)  # defaults to self


def send_notification(summary: dict, new_matches: list[dict]):
    """Send an email summary of the nightly scan."""

    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP not configured (set SMTP_USER and SMTP_PASS). Skipping email.")
        _write_fallback_notification(summary, new_matches)
        return

    new_count = summary.get("new_matches_found", 0)
    total_pending = summary.get("total_pending_review", 0)
    scan_date = summary.get("scan_date", "unknown")

    if new_count == 0 and total_pending == 0:
        logger.info("No matches to report, skipping notification.")
        return

    # ── Build email ──
    subject = f"🔍 Job Search: {new_count} new match(es) found — {scan_date}"
    if new_count == 0:
        subject = f"📋 Job Search: {total_pending} letter(s) still pending review — {scan_date}"

    body_lines = [
        f"Nightly Job Search Scan — {scan_date}",
        f"{'=' * 50}",
        f"",
        f"Companies scanned:       {summary.get('companies_scanned', 0)}",
        f"New matches found:       {new_count}",
        f"Skipped (already applied): {summary.get('skipped_already_applied', 0)}",
        f"Skipped (lateral moves):   {summary.get('skipped_lateral_moves', 0)}",
        f"Total pending review:    {total_pending}",
        f"Scan duration:           {summary.get('elapsed_seconds', 0)}s",
        f"",
    ]

    if new_matches:
        body_lines.append("NEW MATCHES:")
        body_lines.append("-" * 40)
        for m in new_matches:
            body_lines.append(f"  • {m['company']} — {m['role']}")
            if m.get("url"):
                body_lines.append(f"    {m['url']}")
        body_lines.append("")

    body_lines.extend([
        "Open Claude and say:",
        '  "Show me pending job applications"',
        "to review the cover letters and approve submissions.",
        "",
        "— Your Job Search Bot",
    ])

    body = "\n".join(body_lines)

    # ── Send ──
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, NOTIFY_TO, msg.as_string())

        logger.info("Notification email sent to %s", NOTIFY_TO)

    except Exception as e:
        logger.error("Failed to send notification email: %s", e)
        _write_fallback_notification(summary, new_matches)


def _write_fallback_notification(summary: dict, new_matches: list[dict]):
    """Write a notification file as fallback when email isn't configured."""
    from pathlib import Path
    import json

    notif = {
        "type": "scan_complete",
        "scan_date": summary.get("scan_date"),
        "new_matches": len(new_matches),
        "total_pending": summary.get("total_pending_review", 0),
        "message": (
            f"Nightly scan found {len(new_matches)} new match(es). "
            f"{summary.get('total_pending_review', 0)} total pending review. "
            f"Open Claude and say 'Show me pending job applications'."
        ),
        "matches": [
            {"company": m["company"], "role": m["role"]}
            for m in new_matches
        ],
    }

    notif_path = Path("/data/notification.json")
    notif_path.write_text(json.dumps(notif, indent=2, ensure_ascii=False))
    logger.info("Fallback notification written to %s", notif_path)
