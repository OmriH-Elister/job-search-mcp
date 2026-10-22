# AGENTS.md - Job Search Harness

Operating spec for the automated job search pipeline. Authoritative. If this
file and conversational context disagree, ask.

All candidate-specific data (identity, target roles, seed list, exclusions)
lives in `config.json` — see `config.example.json`. Nothing personal belongs
in this file.

---

## Goal

Keep a steady flow of applications going to well-matched roles with minimal
manual effort, gated on a single human decision: cover letter approval.
Everything else — scanning, matching, filtering, drafting, submitting — is
automated.

The pipeline is NOT paused for in-flight offers; it runs full-tilt regardless
of interview status.

---

## Scan workflow

After a scan, silently reject:
1. Companies already applied to (see `data/previously_applied.json`)
2. Lateral moves — role tiers the candidate has configured as below target
3. False positives — product pages, marketing URLs, non-job pages

For each genuinely new match:
1. Draft the cover letter immediately
2. Display it to the candidate
3. Pause ONLY for letter confirmation

Do NOT ask "want me to approve these?" and do NOT list matches separately from
their letters. Once a letter is confirmed, submit immediately:
`approve_application` submits on approval (ATS, falling back to email).

**Level filter:** before role matching, titles containing a
`config.json` `exclusions` term (too senior, or helpdesk/intern-level) are
dropped and listed in the scan log. `override_titles` re-admits exact titles.

---

## Email fallback

When ATS submission fails, the server prints the recipient, subject, letter
and CV path, and records the job as `needs_email` with those details kept in
`data/previously_applied.json`. The server cannot reach Gmail itself, so the
agent session does the rest:

1. Create a Gmail draft with the CV attached for the candidate to review.
2. Call `mark_drafted(job_id, draft_id)` → status `drafted`. Drafted jobs
   block re-matching but do not count as delivered.
3. Once the candidate sends it, call `mark_sent(job_id)` → status `submitted`.

---

## LinkedIn Easy Apply queue

The nightly run calls `linkedin_queue.py build`, which uses LinkedIn's public
(logged-out) job search to queue up to 10 matching Easy Apply jobs. Nothing
is submitted. The `/linkedin-apply` command (`claude/commands/`) reviews the
queue with the candidate and submits approved jobs through Claude in Chrome,
answering screening questions only from `data/linkedin_answers.json` (see
`linkedin_answers.example.json`). Unmatched questions skip the job; answers
are never invented.

**Zero-match runs:** if a full scan yields no genuinely new matches after
filtering, automatically research and add 5 new relevant companies to
`config.json` `company_seed_list` before ending the run. Focus on categories
adjacent to the configured targets (startups, MSSPs, consulting firms, larger
firms with local offices) that are not already listed. The pipeline treats
"no findings" as a coverage gap, not a success state.

---

## Excluded companies

Configured in `config.json` (current employer and any others the candidate
lists). Never apply to an excluded company, even on a strong match.

---

## Infrastructure

| Item | Value |
|---|---|
| Root | repo directory (scripts derive paths from their own location) |
| Image | `job-search-mcp:latest` |
| Data volume | `<root>\data` -> `/data` (nightly AND MCP server must share this) |
| CV volume | `JOB_SEARCH_CV_DIR` env var -> `/cvs:ro` (defaults to `<root>\cvs`) |
| Scheduled task | Windows: `JobSearchNightlyScan`, daily 22:00. Linux: `systemd/job-search-nightly.timer` (or cron) |
| Wrapper | `run_nightly.ps1` (Windows) / `run_nightly.sh` (Linux) |
| MCP launcher | `mcp_launch.sh` (Linux; loads `.env.nightly`) |
| ATS feeds | `modules/ats.py` — detected from the careers page or pinned per company with an `ats` block |
| Seed list | `company_seed_list` in `config.json` |

### SMTP
Gmail app password via user env vars `JOB_SEARCH_SMTP_USER` /
`JOB_SEARCH_SMTP_PASS`. Setup instructions in `setup_scheduled_task.ps1`.
Credentials are never stored in files.

### Known failure modes
- **Docker Desktop not running** — once caused a month of scans to fail
  silently. Wrapper now auto-starts Docker, waits up to 10 min, and fires a
  toast on failure. Never let failures be silent again.
- UTF-8 BOM on `config.json` breaks JSON parse. Write with
  `UTF8Encoding($false)`.
- Em dash / unicode corruption in generated files. Prefer ASCII in scripts.
- Session resume: `initialize_session(resume=true)`.
- The MCP server holds `previously_applied.json` in memory and rewrites the
  whole file on every update. After editing it by hand, restart/reconnect the
  server before using any tool that writes, or the edit is lost.
- Code is baked into the Docker image (only `data/`, `config.json` and the CV
  directory are mounted). Rebuild the image after code changes.
- Task Scheduler may not inherit env vars — wrapper loads them explicitly.

### Config hygiene
`config.json` is authoritative and gitignored. Keep dated backups locally as
`config.json.bak-YYYYMMDD`; they are gitignored too.
