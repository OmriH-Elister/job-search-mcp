import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path("/data/session_state.json")
HISTORY_FILE = Path("/data/previously_applied.json")


class ApplicationHistory:
    """Persists across sessions. Tracks which company+role combos have been applied to."""

    def __init__(self):
        self._applied: dict[str, dict] = {}  # key: "company::role" -> {timestamp, status, ...}
        self.load()

    def _key(self, company: str, role: str) -> str:
        return f"{company.lower().strip()}::{role.lower().strip()}"

    def has_applied(self, company: str, role: str) -> bool:
        return self._key(company, role) in self._applied

    def has_applied_to_company(self, company: str) -> list[str]:
        """Returns list of roles already applied to at this company."""
        prefix = f"{company.lower().strip()}::"
        return [v["role"] for k, v in self._applied.items() if k.startswith(prefix)]

    def record(self, company: str, role: str, status: str, job_id: str = "", method: str = "",
               extra: dict | None = None):
        key = self._key(company, role)
        self._applied[key] = {
            "company": company,
            "role": role,
            "status": status,
            "job_id": job_id,
            "method": method,
            "timestamp": time.time(),
            "date": time.strftime("%Y-%m-%d %H:%M"),
            **(extra or {}),
        }
        self.save()

    def find_by_job_id(self, job_id: str) -> str | None:
        """History key for a job_id, or None."""
        for k, v in self._applied.items():
            if v.get("job_id") == job_id:
                return k
        return None

    def update(self, key: str, fields: dict):
        self._applied[key].update(fields)
        self.save()

    def get_all(self) -> list[dict]:
        return list(self._applied.values())

    # Statuses that mean an application actually reached the employer.
    DELIVERED_STATUSES = {"submitted"}

    def count(self) -> int:
        """Applications actually delivered. Drafts and dead ends do not count."""
        return sum(1 for v in self._applied.values()
                   if v.get("status") in self.DELIVERED_STATUSES)

    def count_tracked(self) -> int:
        """Every company+role pair on file, whatever became of it."""
        return len(self._applied)

    def count_by_status(self) -> dict[str, int]:
        """Breakdown of the history file by status, for reporting."""
        out: dict[str, int] = {}
        for v in self._applied.values():
            out[v.get("status") or "unknown"] = out.get(v.get("status") or "unknown", 0) + 1
        return out

    def save(self):
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text(json.dumps(self._applied, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning("History save failed: %s", e)

    def load(self):
        try:
            if HISTORY_FILE.exists():
                self._applied = json.loads(HISTORY_FILE.read_text())
                logger.info("Loaded application history: %d entries", len(self._applied))
        except Exception as e:
            logger.warning("History load failed: %s", e)
            self._applied = {}


class SessionState:
    def __init__(self):
        self.history = ApplicationHistory()
        self.reset()

    def reset(self):
        self.session_id = str(int(time.time()))
        self.started_at = time.time()
        self.companies = []
        self.scan_index = 0
        self.matched_jobs = []
        self.applications = {}
        self.stats = {
            "companies_total": 0,
            "companies_scanned": 0,
            "companies_with_careers": 0,
            "jobs_found_raw": 0,
            "jobs_matched": 0,
            "skipped_already_applied": 0,
            "skipped_blocked_url": 0,
            "approved": 0,
            "submitted": 0,
        }

    def save(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "session_id": self.session_id,
                "started_at": self.started_at,
                "scan_index": self.scan_index,
                "companies": self.companies,
                "matched_jobs": self.matched_jobs,
                "applications": self.applications,
                "stats": self.stats,
            }
            STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning("State save failed: %s", e)

    def load(self) -> bool:
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                self.session_id = data.get("session_id", self.session_id)
                self.scan_index = data.get("scan_index", 0)
                self.companies = data.get("companies", [])
                self.matched_jobs = data.get("matched_jobs", [])
                self.applications = data.get("applications", {})
                self.stats = {**self.stats, **data.get("stats", {})}
                return True
        except Exception as e:
            logger.warning("State load failed: %s", e)
        return False

    def add_job(self, job: dict):
        if "job_id" not in job:
            return
        self.matched_jobs.append(job)
        self.applications[job["job_id"]] = job
        self.stats["jobs_matched"] += 1

    def update_application(self, job_id: str, updates: dict):
        if job_id in self.applications:
            self.applications[job_id].update(updates)
            for j in self.matched_jobs:
                if j.get("job_id") == job_id:
                    j.update(updates)
                    break

    def summary(self) -> dict:
        pending = [a for a in self.applications.values() if a.get("status") == "pending_approval"]
        submitted = [a for a in self.applications.values() if a.get("status") == "submitted"]
        return {
            "session_id": self.session_id,
            "stats": self.stats,
            "scan_progress": f"{self.scan_index}/{len(self.companies)}",
            "pending_approvals": len(pending),
            "submitted": len(submitted),
            "pending_list": pending,
            "submitted_list": submitted,
            "total_historical_applications": self.history.count(),
            "total_historical_tracked": self.history.count_tracked(),
            "historical_by_status": self.history.count_by_status(),
        }
