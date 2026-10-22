import asyncio
import json
import logging
import sys
import time
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))

Path("/data").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("/data/job_search.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
)
logger = logging.getLogger("job_search")

mcp = FastMCP("job-search")
CONFIG = None
browser = None
state = None
company_finder = None
app_manager = None
careers_scanner = None

PENDING_FILE = Path("/data/pending_review.json")
COMPANY_SCAN_TIMEOUT = 60  # seconds per company — hung sites get skipped


def _ensure_ready():
    global CONFIG, browser, state, company_finder, app_manager, careers_scanner
    if CONFIG is None:
        with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
            CONFIG = json.load(f)
    if state is None:
        from modules.session_state import SessionState
        state = SessionState()
    if browser is None:
        from modules.browser_manager import BrowserManager
        browser = BrowserManager(headless=os.getenv("BROWSER_HEADLESS", "true").lower() == "true")
    if company_finder is None:
        from modules.company_finder import CompanyFinder
        company_finder = CompanyFinder(CONFIG)
    if app_manager is None:
        from modules.application_manager import ApplicationManager
        app_manager = ApplicationManager(CONFIG, browser)
    if careers_scanner is None:
        from modules.careers_scanner import CareersScanner
        careers_scanner = CareersScanner(browser, CONFIG["target_roles"],
                                         CONFIG.get("exclusions"))


# ══════════════════════════════════════════════════════════════════════════════
# Tool 1: Initialize
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def initialize_session(resume: bool = False) -> str:
    """Start a new job search session, or resume a previous one."""
    _ensure_ready()
    await browser.init()
    hist_count = state.history.count()
    if resume and state.load():
        for job in state.matched_jobs:
            if job.get("status") in ("pending_approval", "approved"):
                app_manager._pending[job["job_id"]] = job
        return f"✅ Resumed session {state.session_id} (history: {hist_count} previous applications)"
    state.reset()
    return f"✅ Session {state.session_id} started (history: {hist_count} previous applications)"


# ══════════════════════════════════════════════════════════════════════════════
# Tool 2: Find Companies
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def find_companies() -> str:
    """Compile a list of tech companies that may be hiring."""
    _ensure_ready()
    seed = company_finder.get_seed_companies()
    discovered = await company_finder.search_additional_companies()
    merged = company_finder.merge_and_deduplicate(seed, discovered)
    prioritized = company_finder.prioritize(merged)
    state.companies = prioritized
    state.stats["companies_total"] = len(prioritized)
    state.save()
    lines = [f"Found {len(prioritized)} companies ({len(seed)} seed + {len(discovered)} discovered):"]
    for i, c in enumerate(prioritized[:15]):
        lines.append(f"  {i+1}. {c['name']} ({c['location']}) [{c['source']}]")
    if len(prioritized) > 15:
        lines.append(f"  ... and {len(prioritized) - 15} more")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Tool 3: Scan Careers (with per-company timeout)
# ══════════════════════════════════════════════════════════════════════════════

async def _scan_single_company(company: dict) -> str:
    """Scan one company. Returns a status line."""
    name = company["name"]

    # Find careers page
    careers_url = await careers_scanner.find_careers_url(company)
    if not careers_url:
        company["status"] = "no_careers_page"
        return f"  ❌ {name} — no careers page found"

    state.stats["companies_with_careers"] += 1
    company["careers_url"] = careers_url
    ats_type = careers_scanner.detect_ats(careers_url)

    # Extract job links
    raw_jobs = await careers_scanner.find_job_links(careers_url, name)

    # Fallback to text extraction
    if not raw_jobs:
        page = await browser.new_page()
        try:
            if await browser.navigate(page, careers_url):
                text = await browser.safe_get_text(page)
                raw_jobs = _extract_jobs_from_text(text, name, careers_url, ats_type)
        finally:
            await browser.safe_close_page(page)

    state.stats["jobs_found_raw"] += len(raw_jobs)

    # Match against target roles
    matched = careers_scanner.match_jobs(raw_jobs)
    if not matched:
        company["status"] = "no_match"
        return f"  ⬚ {name} — careers page found, no matching roles"

    # Find HR email for fallback
    hr_email = await careers_scanner.find_hr_email(company)

    new_matches = 0
    skipped = 0
    for job in matched:
        role = job.get("matched_role", "Unknown")
        if state.history.has_applied(name, role):
            skipped += 1
            state.stats["skipped_already_applied"] += 1
            continue

        job["company"] = name
        job["company_url"] = company.get("url", "")
        if not job.get("url"):
            job["url"] = careers_url
        if hr_email:
            job["hr_email"] = hr_email
        if not job.get("ats_type") and ats_type:
            job["ats_type"] = ats_type

        jid = app_manager.queue_for_approval(job)
        state.add_job(app_manager._pending[jid])
        new_matches += 1

    ats_label = f" [ATS: {ats_type}]" if ats_type else ""
    if new_matches > 0 and skipped > 0:
        company["status"] = "matched"
        return f"  ✅ {name} — {new_matches} new, {skipped} skipped (already applied){ats_label}"
    elif new_matches > 0:
        company["status"] = "matched"
        return f"  ✅ {name} — {new_matches} new match(es){ats_label}"
    elif skipped > 0:
        company["status"] = "already_applied"
        return f"  ⏭️ {name} — {skipped} match(es) skipped (already applied)"
    company["status"] = "no_match"
    return f"  ⬚ {name} — no matching roles"


@mcp.tool()
async def scan_careers(batch_size: int = 5) -> str:
    """
    Scan companies with per-company 60s timeout. Hung sites get skipped
    and the scan continues. Filters already-applied roles and blocked URLs.
    """
    _ensure_ready()
    if not state.companies:
        return "❌ No companies loaded. Call find_companies first."

    start = state.scan_index
    end = min(start + batch_size, len(state.companies))
    if start >= len(state.companies):
        return f"✅ All {len(state.companies)} companies scanned."

    results = []
    for i in range(start, end):
        company = state.companies[i]
        logger.info("Scanning %d/%d: %s", i + 1, len(state.companies), company["name"])
        try:
            r = await asyncio.wait_for(
                _scan_single_company(company),
                timeout=COMPANY_SCAN_TIMEOUT
            )
            results.append(r)
        except asyncio.TimeoutError:
            logger.warning("⏱️ %s timed out after %ds", company["name"], COMPANY_SCAN_TIMEOUT)
            company["status"] = "timeout"
            results.append(f"  ⏱️ {company['name']} — timed out, skipped")
        except Exception as e:
            logger.warning("Error scanning %s: %s", company["name"], e)
            company["status"] = "error"
            results.append(f"  ⚠️ {company['name']} — error: {str(e)[:60]}")

    state.scan_index = end
    state.stats["companies_scanned"] = end
    state.save()
    return f"Scanned companies {start + 1}–{end} of {len(state.companies)}:\n" + "\n".join(results)


def _extract_jobs_from_text(text, company, url, ats_type=None):
    from modules.careers_scanner import CareersScanner
    if CareersScanner.is_blocked_url(url):
        return []
    jobs, seen = [], set()
    hints = [
        "analyst", "engineer", "specialist", "administrator", "admin",
        "developer", "security", "soc", "devops", "secops", "support",
        "helpdesk", "linux", "network", "monitoring", "pentester",
        "cyber", "it ", "noc", "אבטחת", "מנהל", "תמיכה", "דרוש",
    ]
    for line in text.split("\n"):
        c = line.strip()
        if not c or len(c) < 5 or len(c) > 120:
            continue
        if any(h in c.lower() for h in hints) and c not in seen:
            seen.add(c)
            jobs.append({
                "title": c, "description": "", "company": company,
                "url": url, "ats_type": ats_type,
                "application_method": f"ats:{ats_type}" if ats_type else "web_form",
            })
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# Tool 4: Review Applications
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def review_applications() -> str:
    """Show matched jobs with draft cover letters, pending approval."""
    _ensure_ready()
    pending = app_manager.get_pending_approvals()
    if not pending:
        return "No applications pending review."
    parts = [f"📋 {len(pending)} application(s) pending review:\n"]
    for j in pending:
        parts.append("═" * 39)
        parts.append(f"Job ID:    {j['job_id']}")
        parts.append(f"Company:   {j.get('company', '?')}")
        parts.append(f"Role:      {j.get('matched_role', j.get('title', '?'))}")
        parts.append(f"CV:        {j.get('cv_file', 'N/A')}")
        parts.append(f"URL:       {j.get('url', 'N/A')}")
        parts.append(f"ATS:       {j.get('ats_type', 'none')}")
        if j.get("hr_email"):
            parts.append(f"HR Email:  {j['hr_email']}")
        parts.append("")
        parts.append("--- Cover Letter ---")
        parts.append(j.get("cover_letter", "(none)"))
        parts.append("")
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Tool 5: Approve Application
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def approve_application(job_id: str) -> str:
    """Approve a specific job application and submit it immediately."""
    _ensure_ready()
    if job_id not in app_manager._pending:
        return f"❌ Job ID '{job_id}' not found."
    job = app_manager._pending[job_id]
    job["status"] = "approved"
    state.update_application(job_id, {"status": "approved"})
    state.stats["approved"] += 1
    state.history.record(
        company=job.get("company", ""), role=job.get("matched_role", ""),
        status="approved", job_id=job_id,
    )
    _update_pending_file_status(job_id, "approved")
    state.save()
    approved_msg = f"✅ Approved: {job.get('matched_role', 'role')} at {job.get('company', 'company')} (ID: {job_id})"
    return f"{approved_msg}\n{await _submit(job_id)}"


# ══════════════════════════════════════════════════════════════════════════════
# Tool 6: Reject Application
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def reject_application(job_id: str, reason: str = "") -> str:
    """Reject a job application."""
    _ensure_ready()
    if job_id not in app_manager._pending:
        return f"❌ Job ID '{job_id}' not found."
    job = app_manager._pending.pop(job_id)
    state.update_application(job_id, {"status": "rejected", "reject_reason": reason})
    _update_pending_file_status(job_id, "rejected")
    state.save()
    return f"🚫 Rejected: {job.get('matched_role', 'role')} at {job.get('company', 'company')}"


# ══════════════════════════════════════════════════════════════════════════════
# Tool 7: Submit Application
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def submit_application(job_id: str) -> str:
    """Submit an approved application. ATS first, email fallback.

    Kept as a standalone tool for retries (e.g. after a submit_failed or
    needs_email status) — approve_application now submits automatically."""
    _ensure_ready()
    if job_id not in app_manager._pending:
        return f"❌ Job ID '{job_id}' not found."
    job = app_manager._pending[job_id]
    if job.get("status") != "approved":
        return f"⚠️ Job '{job_id}' not approved yet."
    return await _submit(job_id)


async def _submit(job_id: str) -> str:
    job = app_manager._pending[job_id]
    result = await app_manager.submit(job_id)

    if result.get("success"):
        state.update_application(job_id, {"status": "submitted", "method": result.get("method")})
        state.stats["submitted"] += 1
        state.history.record(
            company=job.get("company", ""), role=job.get("matched_role", ""),
            status="submitted", job_id=job_id, method=result.get("method", ""),
        )
        _update_pending_file_status(job_id, "submitted")
        app_manager._pending.pop(job_id, None)
        state.save()
        return f"✅ {job.get('company', '')} — {result['message']}"

    if result.get("method") == "needs_email_fallback":
        fb = result["email_fallback"]
        state.update_application(job_id, {"status": "needs_email"})
        # Keep the letter and addresses: the pending file entry is removed below,
        # and this record is the only place they survive until the draft exists.
        state.history.record(
            company=job.get("company", ""), role=job.get("matched_role", ""),
            status="needs_email", job_id=job_id, method="email_fallback",
            extra={"email_fallback": {
                "to_email": fb["to_email"],
                "constructed_emails": fb["constructed_emails"],
                "subject": fb["subject"],
                "body": fb["body"],
                "cv_filename": fb["cv_filename"],
                "url": job.get("url", ""),
            }},
        )
        _update_pending_file_status(job_id, "needs_email")
        state.save()
        lines = [
            f"⚠️ ATS failed for {job.get('company', '')} ({result.get('message', '')})",
            "", "📧 EMAIL FALLBACK:",
            f"  To:        {fb['to_email'] or 'NO ADDRESS FOUND ON SITE'}",
        ]
        if not fb["to_email"] and fb["constructed_emails"]:
            lines += [
                "             ⚠️  The addresses below are GUESSES built from the",
                "                 domain. None was found on the site. Verify one",
                "                 exists before sending — most companies of this",
                "                 kind take applications only through their form.",
            ]
            for addr in fb["constructed_emails"]:
                lines.append(f"                 {addr}  (unverified)")
        lines += [
            f"  Subject:   {fb['subject']}",
            f"  CV:        {fb['cv_filename']}",
            f"  CV Path:   {fb['cv_path']}",
            "", "--- Letter Body ---", fb["body"],
            "", "--- Next step ---",
            "Create a Gmail draft with the CV attached for the candidate to review, then call",
            f"mark_drafted(job_id=\"{job_id}\", draft_id=<Gmail draft id>).",
            f"Once the candidate sends it, call mark_sent(job_id=\"{job_id}\").",
        ]
        return "\n".join(lines)

    state.update_application(job_id, {"status": "submit_failed", "error": result.get("message")})
    state.save()
    return f"❌ {job.get('company', '')} — {result.get('message', 'Unknown error')}"


# ══════════════════════════════════════════════════════════════════════════════
# Tool 7b: Email fallback tracking (drafted -> submitted)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def mark_drafted(job_id: str, draft_id: str = "") -> str:
    """Record that a Gmail draft exists for an email-fallback application.

    Call right after creating the draft. 'drafted' blocks re-matching but does
    not count as delivered."""
    _ensure_ready()
    key = state.history.find_by_job_id(job_id)
    if not key:
        return f"❌ Job ID '{job_id}' not found in history."
    state.history.update(key, {
        "status": "drafted", "method": "email_fallback",
        "draft_id": draft_id, "drafted_date": time.strftime("%Y-%m-%d %H:%M"),
    })
    state.update_application(job_id, {"status": "drafted"})
    _update_pending_file_status(job_id, "drafted")
    state.save()
    rec = state.history._applied[key]
    return f"📝 Drafted: {rec['role']} at {rec['company']} (draft {draft_id or 'id not given'})"


@mcp.tool()
async def mark_sent(job_id: str) -> str:
    """Record that a drafted email-fallback application was sent."""
    _ensure_ready()
    key = state.history.find_by_job_id(job_id)
    if not key:
        return f"❌ Job ID '{job_id}' not found in history."
    state.history.update(key, {
        "status": "submitted", "method": "email_fallback",
        "sent_date": time.strftime("%Y-%m-%d %H:%M"),
    })
    state.update_application(job_id, {"status": "submitted"})
    state.save()
    rec = state.history._applied[key]
    return f"✅ Sent: {rec['role']} at {rec['company']}"


# ══════════════════════════════════════════════════════════════════════════════
# Tool 8: Get Status
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_status() -> str:
    """Session and historical stats."""
    _ensure_ready()
    s = state.summary()
    nightly_pending = 0
    if PENDING_FILE.exists():
        try:
            nightly_pending = len([i for i in json.loads(PENDING_FILE.read_text())
                                   if i.get("status") == "pending_review"])
        except Exception:
            pass
    lines = [
        f"📊 Session {s['session_id']} Status", "",
        f"  Companies total:          {s['stats']['companies_total']}",
        f"  Companies scanned:        {s['stats']['companies_scanned']}",
        f"  With careers pages:        {s['stats']['companies_with_careers']}",
        f"  Raw jobs found:            {s['stats']['jobs_found_raw']}",
        f"  Matched to your roles:     {s['stats']['jobs_matched']}",
        f"  Skipped (already applied): {s['stats'].get('skipped_already_applied', 0)}",
        f"  Approved:                  {s['stats']['approved']}",
        f"  Submitted:                 {s['stats']['submitted']}",
        f"  Scan progress:             {s['scan_progress']}",
        f"  Pending approval:          {s['pending_approvals']}",
        "",
        f"📁 Applications delivered:   {s.get('total_historical_applications', 0)}",
        f"📇 Company+role pairs known: {s.get('total_historical_tracked', 0)}",
    ]

    undelivered = {k: v for k, v in s.get("historical_by_status", {}).items()
                   if k != "submitted"}
    if undelivered:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(undelivered.items()))
        lines.append(f"   ↳ not delivered:          {detail}")

    lines.append(f"🌙 Nightly scan pending:     {nightly_pending} letter(s) awaiting review")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Tool 9: Review Pending (nightly scan results)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def review_pending() -> str:
    """Show nightly scan results pending review."""
    _ensure_ready()
    if not PENDING_FILE.exists():
        return "📭 No pending nightly results. Scan runs at 10 PM."

    try:
        pending = [p for p in json.loads(PENDING_FILE.read_text())
                   if p.get("status") == "pending_review"]
    except Exception as e:
        return f"❌ Error reading pending file: {e}"

    if not pending:
        return "📭 All caught up!"

    # Load into app_manager so approve/reject/submit work
    for p in pending:
        jid = p["job_id"]
        if jid not in app_manager._pending:
            app_manager._pending[jid] = {
                "job_id": jid, "company": p["company"],
                "matched_role": p["role"], "cv_file": p.get("cv_file", ""),
                "url": p.get("url", ""), "ats_type": p.get("ats_type"),
                "hr_email": p.get("hr_email"), "company_url": p.get("company_url", ""),
                "cover_letter": p.get("cover_letter", ""),
                "status": "pending_approval",
                "application_method": f"ats:{p['ats_type']}" if p.get("ats_type") else "web_form",
            }

    # Last scan summary header
    summary_file = Path("/data/last_scan_summary.json")
    header = []
    if summary_file.exists():
        try:
            s = json.loads(summary_file.read_text())
            header = [
                f"🌙 Last nightly scan: {s.get('scan_date', '?')}",
                f"   Companies scanned: {s.get('companies_scanned', '?')}",
                f"   New matches: {s.get('new_matches_found', '?')}",
                f"   Skipped: {s.get('skipped_already_applied', 0)}", "",
            ]
        except Exception:
            pass

    parts = header + [f"📋 {len(pending)} application(s) pending review:\n"]
    for p in pending:
        parts.append("═" * 39)
        parts.append(f"Job ID:    {p['job_id']}")
        parts.append(f"Company:   {p['company']}")
        parts.append(f"Role:      {p['role']}")
        parts.append(f"CV:        {p.get('cv_file', 'N/A')}")
        parts.append(f"URL:       {p.get('url', 'N/A')}")
        if p.get("ats_type"):
            parts.append(f"ATS:       {p['ats_type']}")
        if p.get("hr_email"):
            parts.append(f"HR Email:  {p['hr_email']}")
        parts.append(f"Found:     {p.get('scanned_at', '?')}")
        parts.append("")
        parts.append("--- Cover Letter ---")
        parts.append(p.get("cover_letter", "(none)"))
        parts.append("")
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Helper: Update pending_review.json
# ══════════════════════════════════════════════════════════════════════════════

def _update_pending_file_status(job_id: str, new_status: str):
    if not PENDING_FILE.exists():
        return
    try:
        items = json.loads(PENDING_FILE.read_text())
        updated = [i for i in items
                   if not (i["job_id"] == job_id and new_status in ("rejected", "submitted", "needs_email", "drafted"))]
        PENDING_FILE.write_text(json.dumps(updated, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.warning("Pending file update failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run(transport="stdio")