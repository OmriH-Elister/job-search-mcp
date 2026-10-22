#!/usr/bin/env python3
"""
LinkedIn Easy Apply queue: the nightly run fills it, /linkedin-apply submits it.

Nothing here logs in to LinkedIn or submits anything. `build` uses the public
(logged-out) job search; submission happens later in an interactive Claude
session, after the candidate approves each job.

Usage:
  linkedin_queue.py build            # top the queue up to QUEUE_SIZE pending jobs
                                     # (plus up to config remote.max_in_queue remote jobs)
  linkedin_queue.py list [--all]     # show pending jobs (or every job) as JSON
  linkedin_queue.py check JOB_ID     # exit 1 if the company was applied to since queueing
  linkedin_queue.py mark JOB_ID STATUS [NOTE]
                                     # STATUS: rejected | skipped | failed
  linkedin_queue.py record JOB_ID    # mark submitted + add to previously_applied.json
"""
import fcntl
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "config.json"
QUEUE_FILE = DATA_DIR / "linkedin_queue.json"
APPLIED_FILE = DATA_DIR / "previously_applied.json"
PENDING_REVIEW_FILE = DATA_DIR / "pending_review.json"
APPLIED_LOCK = DATA_DIR / ".applied.lock"  # shared with sync_applied_jobslist.sh

QUEUE_SIZE = 10
PENDING_EXPIRY_DAYS = 7       # LinkedIn postings close fast; stale ones get dropped
SEEN_RETENTION_DAYS = 60      # how long a rejected/expired job id stays blocked
REQUEST_DELAY_SECONDS = 3


# LinkedIn spells some cities differently from config.json geography
LOCATION_ALIASES = [
    "center district", "central district", "tel aviv district", "tel aviv-yafo",
    "petah tiqwa", "petah tikva", "rishon letsiyon", "rishon lezion", "ramat gan",
    "bne berak", "giv'at shmuel", "rosh ha'ayin", "nes tsiyona",
]

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"

# Remote jobs: the config.json "remote" block turns this on.
# A remote posting is dropped when its description limits applicants to somewhere other
# than Israel; otherwise it is queued with a note saying whether Israel was confirmed.
OK_PLACES = r"israel|emea|middle east|worldwide|anywhere|global"
RESTRICTED_TO = re.compile(
    r"(?:must|need to|required to|should)\s+(?:be\s+)?(?:based|located|reside|residing|live|living)"
    r"\s+(?:in|within)\s+(?:the\s+)?([a-z][a-z &/-]{1,40})"
    r"|(?:only|exclusively)\s+(?:open|available)\s+to\s+(?:candidates|applicants|residents)"
    r"\s+(?:in|from|based in|located in)\s+(?:the\s+)?([a-z][a-z &/-]{1,40})"
    r"|(?:legally\s+)?authori[sz]ed\s+to\s+work\s+in\s+(?:the\s+)?([a-z]+(?:\s[a-z]+)?)"
    r"|\b([a-z]+(?:\s[a-z]+)?)\s+residents\s+only",
    re.IGNORECASE)
CONFIRMS_ISRAEL = re.compile(rf"\b(?:{OK_PLACES})\b", re.IGNORECASE)
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ── Helpers ──────────────────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def norm_company(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[.,]", "", name)
    name = re.sub(r"\b(ltd|inc|llc|israel|il|group|technologies|technology)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def has_term(text: str, term: str) -> bool:
    # Same boundary rule as CareersScanner.out_of_reach (\w covers Hebrew)
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


@contextmanager
def applied_lock():
    with open(APPLIED_LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load_queue() -> dict:
    return load_json(QUEUE_FILE, {"jobs": [], "seen": {}})


def find_job(queue: dict, job_id: str) -> dict:
    for job in queue["jobs"]:
        if job["job_id"] == job_id:
            return job
    sys.exit(f"ERROR: job {job_id} not in queue")


def applied_companies() -> set[str]:
    applied = load_json(APPLIED_FILE, {})
    companies = {norm_company(e.get("company", "")) for e in applied.values()}
    for item in load_json(PENDING_REVIEW_FILE, []):
        if isinstance(item, dict) and item.get("company"):
            companies.add(norm_company(item["company"]))
    return companies


# ── LinkedIn public search ───────────────────────────────────────────────────

CARD = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"(.*?)(?=data-entity-urn=|\Z)', re.S)


def text_of(block: str, pattern: str) -> str:
    m = re.search(pattern, block, re.S)
    if not m:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def search(keywords: str, remote: bool = False, location: str = "Israel") -> list[dict]:
    query = {
        "keywords": keywords,
        "location": location,
        "f_AL": "true",          # Easy Apply only
        "f_TPR": "r604800",      # posted in the past week
        "start": 0,
    }
    if remote:
        query["f_WT"] = "2"      # remote only
    params = urllib.parse.urlencode(query)
    req = urllib.request.Request(f"{SEARCH_URL}?{params}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        page = resp.read().decode("utf-8", errors="replace")

    jobs = []
    for job_id, block in CARD.findall(page):
        jobs.append({
            "job_id": job_id,
            "title": text_of(block, r'base-search-card__title">(.*?)</h3>'),
            "company": text_of(block, r'base-search-card__subtitle">(.*?)</h4>'),
            "location": text_of(block, r'job-search-card__location">(.*?)</span>'),
            "posted": (re.search(r'<time[^>]*datetime="([^"]+)"', block) or [None, ""])[1],
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        })
    return jobs


# ── Filtering ────────────────────────────────────────────────────────────────

def match_role(title: str, target_roles: list[dict]):
    for role in target_roles:
        if any(has_term(title, kw) for kw in role.get("keywords", [])):
            return role
    return None


def in_area(location: str, areas: list[str]) -> bool:
    loc = location.lower()
    if loc.strip() in ("israel", ""):
        return True
    return any(a.lower() in loc for a in areas) or any(a in loc for a in LOCATION_ALIASES)


def fetch_description(job_id: str) -> str | None:
    """Plain text of a posting's description, or None if LinkedIn refuses (e.g. 429)."""
    for attempt in range(2):
        try:
            req = urllib.request.Request(POSTING_URL.format(job_id), headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                page = resp.read().decode("utf-8", errors="replace")
            return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())
        except Exception as e:
            if attempt == 0 and "429" in str(e):
                time.sleep(30)  # rate limited: one slow retry, then give up
                continue
            return None
    return None


def remote_verdict(description: str | None):
    """(reject_reason or None, note) for a remote posting's description."""
    if description is None:
        return None, "Remote; Israel not confirmed (description unavailable)"
    for m in RESTRICTED_TO.finditer(description):
        place = next(g for g in m.groups() if g).strip(" .,")
        if not CONFIRMS_ISRAEL.search(place):
            return f"remote restricted to {place[:30]}", ""
    if CONFIRMS_ISRAEL.search(description):
        return None, "Remote; Israel/EMEA/anywhere mentioned"
    return None, "Remote; Israel not confirmed"


def reject_reason(job, config, exclude_terms, blocked_companies, known_ids, remote=False):
    if job["job_id"] in known_ids:
        return "already seen"
    company = norm_company(job["company"])
    if any(norm_company(ex) in company for ex in config.get("excluded_companies", [])):
        return "excluded company"
    if company in blocked_companies:
        return "already applied"
    for term in exclude_terms:
        if has_term(job["title"], term):
            return f"out of reach ({term})"
    if not match_role(job["title"], config["target_roles"]):
        return "no target role match"
    if not remote and not in_area(job["location"], config["geography"]["search_areas"]):
        return "outside area"
    return None


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_build():
    config = load_json(CONFIG_FILE, None)
    if config is None:
        sys.exit(f"ERROR: cannot read {CONFIG_FILE}")
    excl = config.get("exclusions", {})
    exclude_terms = excl.get("seniority_terms", []) + excl.get("lateral_terms", [])
    override = {t.lower().strip() for t in excl.get("override_titles", [])}

    queue = load_queue()
    today = datetime.now()

    # Expire stale pending jobs, forget old seen ids
    for job in queue["jobs"]:
        queued = datetime.strptime(job["queued_at"], "%Y-%m-%d %H:%M")
        if job["status"] == "pending" and today - queued > timedelta(days=PENDING_EXPIRY_DAYS):
            job["status"] = "expired"
            queue["seen"][job["job_id"]] = now_str()
    cutoff = today - timedelta(days=SEEN_RETENTION_DAYS)
    queue["seen"] = {k: v for k, v in queue["seen"].items()
                     if datetime.strptime(v, "%Y-%m-%d %H:%M") > cutoff}
    queue["jobs"] = [j for j in queue["jobs"] if j["status"] == "pending"
                     or datetime.strptime(j["queued_at"], "%Y-%m-%d %H:%M") > cutoff]

    pending = [j for j in queue["jobs"] if j["status"] == "pending"]
    known_ids = {j["job_id"] for j in queue["jobs"]} | set(queue["seen"])
    blocked = applied_companies() | {norm_company(j["company"]) for j in pending}

    added, stats = [], {}
    for role in config["target_roles"]:
        if len(pending) + len(added) >= QUEUE_SIZE:
            break
        try:
            results = search(role["title"])
        except Exception as e:
            print(f"  search '{role['title']}' failed: {e}", file=sys.stderr)
            continue
        for job in results:
            if len(pending) + len(added) >= QUEUE_SIZE:
                break
            terms = [] if job["title"].lower().strip() in override else exclude_terms
            reason = reject_reason(job, config, terms, blocked, known_ids)
            if reason:
                stats[reason] = stats.get(reason, 0) + 1
                continue
            matched = match_role(job["title"], config["target_roles"])
            job.update({"role": matched["title"], "cv": matched.get("cv", ""),
                        "queued_at": now_str(), "status": "pending", "note": ""})
            added.append(job)
            known_ids.add(job["job_id"])
            blocked.add(norm_company(job["company"]))
        time.sleep(REQUEST_DELAY_SECONDS)

    # ── Remote pass: postings that list Israel as an eligible location ────────
    remote_cfg = config.get("remote", {})
    if remote_cfg.get("enabled"):
        cap = remote_cfg.get("max_in_queue", 3)
        n_remote = sum(1 for j in pending + added if j.get("remote"))
        for role in config["target_roles"]:
            if n_remote >= cap or len(pending) + len(added) >= QUEUE_SIZE:
                break
            try:
                results = search(role["title"], remote=True,
                                 location=remote_cfg.get("search_location", "Israel"))
            except Exception as e:
                print(f"  remote search '{role['title']}' failed: {e}", file=sys.stderr)
                continue
            for job in results:
                if n_remote >= cap or len(pending) + len(added) >= QUEUE_SIZE:
                    break
                terms = [] if job["title"].lower().strip() in override else exclude_terms
                reason = reject_reason(job, config, terms, blocked, known_ids, remote=True)
                if not reason and remote_cfg.get("check_description", True):
                    time.sleep(REQUEST_DELAY_SECONDS)
                    reason, note = remote_verdict(fetch_description(job["job_id"]))
                else:
                    note = "Remote"
                if reason:
                    stats[f"remote: {reason}"] = stats.get(f"remote: {reason}", 0) + 1
                    if reason.startswith("remote restricted"):
                        queue["seen"][job["job_id"]] = now_str()  # don't re-check it nightly
                    continue
                matched = match_role(job["title"], config["target_roles"])
                job.update({"role": matched["title"], "cv": matched.get("cv", ""),
                            "queued_at": now_str(), "status": "pending",
                            "remote": True, "note": note})
                added.append(job)
                n_remote += 1
                known_ids.add(job["job_id"])
                blocked.add(norm_company(job["company"]))
            time.sleep(REQUEST_DELAY_SECONDS)

    queue["jobs"].extend(added)
    save_json(QUEUE_FILE, queue)

    print(f"Queued {len(added)} new, {len(pending) + len(added)} pending total.")
    for reason, n in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {n}: {reason}")
    for job in added:
        print(f"  + {job['company']} - {job['title']} ({job['location']})")
    # Last line is machine-read by run_nightly.sh
    print(f"NEW={len(added)}")


def cmd_list(show_all: bool):
    jobs = load_queue()["jobs"]
    if not show_all:
        jobs = [j for j in jobs if j["status"] == "pending"]
    print(json.dumps(jobs, indent=2, ensure_ascii=False))


def cmd_check(job_id: str):
    job = find_job(load_queue(), job_id)
    if norm_company(job["company"]) in applied_companies():
        print(f"ALREADY APPLIED: {job['company']}")
        sys.exit(1)
    print("OK")


def cmd_mark(job_id: str, status: str, note: str):
    if status not in ("rejected", "skipped", "failed"):
        sys.exit("ERROR: STATUS must be rejected, skipped or failed (use `record` for submitted)")
    queue = load_queue()
    job = find_job(queue, job_id)
    job.update({"status": status, "note": note, "updated_at": now_str()})
    queue["seen"][job_id] = now_str()
    save_json(QUEUE_FILE, queue)
    print(f"{job['company']} - {job['title']}: {status}")


def cmd_record(job_id: str):
    queue = load_queue()
    job = find_job(queue, job_id)

    with applied_lock():
        applied = load_json(APPLIED_FILE, None)
        if applied is None:
            sys.exit(f"ERROR: {APPLIED_FILE} is missing or invalid; not recording")
        key = f"{job['company'].lower()}::{job['title'].lower()}"
        applied[key] = {
            "company": job["company"],
            "role": job["title"],
            "status": "submitted",
            "job_id": job_id,
            "method": "linkedin_easy_apply",
            "timestamp": time.time(),
            "date": now_str(),
        }
        save_json(APPLIED_FILE, applied)

    job.update({"status": "submitted", "updated_at": now_str()})
    queue["seen"][job_id] = now_str()
    save_json(QUEUE_FILE, queue)
    print(f"Recorded: {key}")


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "build":
        cmd_build()
    elif cmd == "list":
        cmd_list("--all" in args)
    elif cmd == "check" and len(args) == 2:
        cmd_check(args[1])
    elif cmd == "mark" and len(args) in (3, 4):
        cmd_mark(args[1], args[2], args[3] if len(args) == 4 else "")
    elif cmd == "record" and len(args) == 2:
        cmd_record(args[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
