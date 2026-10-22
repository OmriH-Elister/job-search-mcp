#!/usr/bin/env python3
"""
Nightly headless job search scan.
Runs the full pipeline: find companies → scan careers → match → draft letters.
Saves results to /data/pending_review.json for Claude to present later.
Sends email notification if new matches are found.
"""

import asyncio
import json
import logging
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Logging ──────────────────────────────────────────────────────────────────
Path("/data").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("/data/nightly_scan.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
)
logger = logging.getLogger("nightly_scan")

PENDING_FILE = Path("/data/pending_review.json")
SCAN_BATCH_SIZE = 5
SCAN_TIMEOUT_SECONDS = 180  # 3 minutes per batch max


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def load_pending() -> list[dict]:
    """Load existing pending reviews (from previous nights)."""
    try:
        if PENDING_FILE.exists():
            return json.loads(PENDING_FILE.read_text())
    except Exception:
        pass
    return []


def save_pending(pending: list[dict]):
    PENDING_FILE.write_text(json.dumps(pending, indent=2, ensure_ascii=False))


async def run_scan():
    logger.info("═══ Nightly scan starting ═══")
    start_time = time.time()

    # ── Load modules ──
    config = load_config()

    from modules.session_state import SessionState, ApplicationHistory
    from modules.browser_manager import BrowserManager
    from modules.company_finder import CompanyFinder
    from modules.careers_scanner import CareersScanner
    from modules.application_manager import ApplicationManager

    state = SessionState()
    state.reset()
    history = state.history

    browser = BrowserManager(headless=True)
    await browser.init()

    company_finder = CompanyFinder(config)
    careers_scanner = CareersScanner(browser, config["target_roles"],
                                     config.get("exclusions"))
    app_manager = ApplicationManager(config, browser)

    # ── Lateral move filter ──
    skip_roles = {
        "support specialist", "support engineer", "help desk", "helpdesk",
        "help desk specialist", "technical support", "it support",
        "desktop support", "customer support",
    }

    # ── Step 1: Find companies ──
    logger.info("Step 1: Finding companies...")
    seed = company_finder.get_seed_companies()
    discovered = await company_finder.search_additional_companies()
    merged = company_finder.merge_and_deduplicate(seed, discovered)
    companies = company_finder.prioritize(merged)
    logger.info("Found %d companies (%d seed + %d discovered)", len(companies), len(seed), len(discovered))

    # ── Step 2: Scan careers in batches ──
    logger.info("Step 2: Scanning careers pages...")
    new_matches = []
    skipped_applied = 0
    skipped_lateral = 0
    skipped_blocked = 0
    companies_scanned = 0
    companies_with_careers = 0

    consecutive_failures = 0

    for batch_start in range(0, len(companies), SCAN_BATCH_SIZE):
        batch_end = min(batch_start + SCAN_BATCH_SIZE, len(companies))
        batch = companies[batch_start:batch_end]

        for company in batch:
            company_name = company["name"]
            companies_scanned += 1
            logger.info("  Scanning %d/%d: %s", companies_scanned, len(companies), company_name)

            try:
                # Find careers page (a pinned ATS needs no page at all)
                if company.get("ats"):
                    careers_url = company.get("careers_url") or company.get("url")
                else:
                    careers_url = await asyncio.wait_for(
                        careers_scanner.find_careers_url(company),
                        timeout=30
                    )
                if not careers_url:
                    continue

                companies_with_careers += 1
                ats_type = careers_scanner.detect_ats(careers_url)

                # Extract job links
                raw_jobs = await asyncio.wait_for(
                    careers_scanner.find_job_links(careers_url, company_name, company),
                    timeout=45
                )

                # Fallback to text extraction. Not for ATS-backed pages: an empty
                # feed means no local postings, and the page text is only noise.
                if not raw_jobs and not careers_scanner.last_ats:
                    page = await browser.new_page()
                    try:
                        if await browser.navigate(page, careers_url):
                            page_text = await browser.safe_get_text(page)
                            raw_jobs = _extract_jobs_from_text(
                                page_text, company_name, careers_url, ats_type
                            )
                    finally:
                        await page.close()

                # Match against target roles
                matched = careers_scanner.match_jobs(raw_jobs)

                if matched:
                    hr_email = None
                    try:
                        hr_email = await asyncio.wait_for(
                            careers_scanner.find_hr_email(company),
                            timeout=15
                        )
                    except asyncio.TimeoutError:
                        pass

                    # Deduplicate: track seen company+role combos within this scan
                    seen_this_scan = set()

                    for job in matched:
                        role = job.get("matched_role", "Unknown")
                        dedup_key = f"{company_name}::{role}"

                        # Skip if already applied (cross-session)
                        if history.has_applied(company_name, role):
                            skipped_applied += 1
                            continue

                        # Skip lateral moves
                        if role.lower() in skip_roles:
                            skipped_lateral += 1
                            continue

                        # Skip within-scan duplicates
                        if dedup_key in seen_this_scan:
                            continue
                        seen_this_scan.add(dedup_key)

                        # Enrich job data
                        job["company"] = company_name
                        job["company_url"] = company.get("url", "")
                        if not job.get("url"):
                            job["url"] = careers_url
                        if hr_email:
                            job["hr_email"] = hr_email
                        if not job.get("ats_type") and ats_type:
                            job["ats_type"] = ats_type

                        # Draft cover letter
                        job_id = app_manager.queue_for_approval(job)
                        job_with_letter = app_manager._pending[job_id]

                        new_matches.append({
                            "job_id": job_id,
                            "company": company_name,
                            "role": role,
                            "cv_file": job.get("cv_file", ""),
                            "url": job.get("url", ""),
                            "ats_type": job.get("ats_type"),
                            "hr_email": job.get("hr_email"),
                            "company_url": job.get("company_url", ""),
                            "cover_letter": job_with_letter.get("cover_letter", ""),
                            "status": "pending_review",
                            "scanned_at": time.strftime("%Y-%m-%d %H:%M"),
                        })

            except asyncio.TimeoutError:
                logger.warning("  Timeout scanning %s, skipping", company_name)
            except Exception as e:
                logger.warning("  Error scanning %s: %s", company_name, e)

    # ── Step 3: Save results ──
    existing_pending = load_pending()

    # Merge with existing pending (don't duplicate)
    existing_keys = {f"{p['company']}::{p['role']}" for p in existing_pending}
    truly_new = [m for m in new_matches if f"{m['company']}::{m['role']}" not in existing_keys]

    all_pending = existing_pending + truly_new
    save_pending(all_pending)

    elapsed = time.time() - start_time

    # ── Step 4: Build summary ──
    summary = {
        "scan_date": time.strftime("%Y-%m-%d %H:%M"),
        "elapsed_seconds": round(elapsed),
        "companies_total": len(companies),
        "companies_scanned": companies_scanned,
        "companies_with_careers": companies_with_careers,
        "new_matches_found": len(truly_new),
        "skipped_already_applied": skipped_applied,
        "skipped_lateral_moves": skipped_lateral,
        "skipped_false_positives": skipped_blocked,
        "total_pending_review": len(all_pending),
        "matches": [
            {"company": m["company"], "role": m["role"], "url": m["url"]}
            for m in truly_new
        ],
    }

    # Save summary for notification script
    summary_path = Path("/data/last_scan_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    logger.info("═══ Scan complete ═══")
    logger.info("  Companies scanned: %d", companies_scanned)
    logger.info("  New matches: %d", len(truly_new))
    logger.info("  Skipped (already applied): %d", skipped_applied)
    logger.info("  Skipped (lateral moves): %d", skipped_lateral)
    logger.info("  Skipped (false positive URLs): %d", skipped_blocked)
    logger.info("  Total pending review: %d", len(all_pending))
    logger.info("  Elapsed: %ds", round(elapsed))

    # ── Step 5: Send notification ──
    if truly_new or all_pending:
        try:
            from modules.notify import send_notification
            send_notification(summary, truly_new)
        except Exception as e:
            logger.warning("Notification failed: %s", e)

    # Cleanup
    try:
        await browser.close()
    except Exception:
        pass

    return summary


def _extract_jobs_from_text(text, company, url, ats_type=None):
    """Same fallback as server.py."""
    from modules.careers_scanner import CareersScanner
    if CareersScanner.is_blocked_url(url):
        return []

    jobs = []
    seen = set()
    role_hints = [
        "analyst", "engineer", "specialist", "administrator", "admin",
        "developer", "security", "soc", "devops", "secops", "support",
        "helpdesk", "linux", "network", "monitoring", "pentester",
        "cyber", "it ", "noc",
    ]
    for line in text.split("\n"):
        clean = line.strip()
        if not clean or len(clean) < 5 or len(clean) > 120:
            continue
        lower = clean.lower()
        if any(h in lower for h in role_hints) and clean not in seen:
            seen.add(clean)
            jobs.append({
                "title": clean, "description": "", "company": company,
                "url": url, "ats_type": ats_type,
                "application_method": f"ats:{ats_type}" if ats_type else "web_form",
            })
    return jobs


if __name__ == "__main__":
    asyncio.run(run_scan())
