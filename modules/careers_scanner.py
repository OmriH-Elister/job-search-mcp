import logging
import re
import asyncio
from urllib.parse import urljoin, urlparse
from typing import Optional
from modules.browser_manager import BrowserManager

logger = logging.getLogger(__name__)

# ── Known ATS domains ─────────────────────────────────────────────────────────
ATS_PATTERNS = {
    "greenhouse": ["greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"],
    "comeet":     ["comeet.com", "comeet.co"],
    "lever":      ["jobs.lever.co", "lever.co"],
    "workday":    ["myworkdayjobs.com", "workday.com"],
    "smartrecruiters": ["smartrecruiters.com"],
}

# ── URL path segments that are NEVER job listings ──────────────────────────────
BLOCKED_PATH_SEGMENTS = {
    # Product & marketing pages
    "/blog", "/blog/", "/solutions/", "/platform/", "/products/",
    "/services/", "/why-", "/glossary", "/glossary/", "/cyberpedia",
    "/cyberpedia/", "/cybersecurity-101", "/cybersecurity-masterclass",
    "/resources/", "/webinars/", "/events/", "/press/", "/news/",
    "/media/", "/partners/", "/pricing/", "/demo/", "/contact",
    "/about/", "/about-us/", "/company/", "/leadership/", "/investors/",
    "/legal/", "/privacy/", "/terms/", "/support/", "/documentation/",
    "/docs/", "/help/", "/faq/", "/knowledge-base/",
    # Reports & whitepapers
    "/security-report", "/report/", "/whitepaper/", "/research/",
    "/annual-report", "/threat-report", "/survey/",
    # Non-careers functional pages
    "/login", "/signup", "/register", "/dashboard/", "/portal/",
    "/managed-support-services/", "/managed-services/", "/cortex",
    "/nam-cxo-portfolio", "/small-business/", "/global-services/",
    "/purple/", "/services-overview/", "/assess-secops-efficacy",
    "/industry/", "/security-foundations/", "/technical-support/",
    # Date-based URLs (typically blog posts / advisories)
    "/threat-advisories", "/attack-reports/", "/advisory/",
    # Misc content pages
    "/customer-stories/", "/case-studies/", "/testimonials/",
    "/use-cases/", "/integrations/", "/marketplace/",
}

# Regex for date-patterned URLs like /cybersecurity-brief-02-07-2020
DATE_URL_PATTERN = re.compile(r'/[\w-]+-\d{2}-\d{2}-\d{4}')


class CareersScanner:
    def __init__(self, browser: BrowserManager, target_roles: list[dict]):
        self.browser = browser
        self.target_roles = target_roles

    # ── URL classification ─────────────────────────────────────────────────

    @staticmethod
    def is_blocked_url(url: str) -> bool:
        """Check if a URL matches known non-job-listing patterns."""
        if not url:
            return True
        path = urlparse(url).path.lower()

        for segment in BLOCKED_PATH_SEGMENTS:
            if segment in path:
                return True

        if DATE_URL_PATTERN.search(path):
            return True

        return False

    @staticmethod
    def is_likely_careers_url(url: str) -> bool:
        """Check if a URL looks like an actual careers/jobs page."""
        if not url:
            return False
        path = urlparse(url).path.lower()
        domain = urlparse(url).netloc.lower()

        careers_signals = [
            "/careers", "/jobs", "/positions", "/openings",
            "/join", "/work-with-us", "/apply", "/vacancies",
            "/hiring", "/open-roles", "/career",
            "/קריירה", "/משרות", "/דרושים",
        ]

        for ats_patterns in ATS_PATTERNS.values():
            if any(p in domain for p in ats_patterns):
                return True

        return any(sig in path for sig in careers_signals)

    # ── ATS detection ──────────────────────────────────────────────────────

    def detect_ats(self, url: str) -> Optional[str]:
        if not url:
            return None
        domain = urlparse(url).netloc.lower()
        for ats_name, patterns in ATS_PATTERNS.items():
            if any(p in domain for p in patterns):
                return ats_name
        return None

    # ── Find careers page ──────────────────────────────────────────────────

    async def find_careers_url(self, company: dict) -> Optional[str]:
        base_url = company.get("url")
        if not base_url:
            return None
        if not base_url.startswith("http"):
            base_url = "https://" + base_url

        page = await self.browser.new_page()
        try:
            if await self.browser.navigate(page, base_url):
                keywords = ["careers", "jobs", "join us", "דרושים", "קריירה", "open positions"]
                href = await self.browser.find_link_by_text(page, keywords)
                if href:
                    full_url = urljoin(base_url, href)
                    if not self.is_blocked_url(full_url):
                        return full_url

            for path in ["/careers", "/jobs", "/positions", "/en/careers"]:
                url = urljoin(base_url, path)
                if await self.browser.navigate(page, url, timeout=5000):
                    return url
        finally:
            await page.close()
        return None

    # ── Extract job links ──────────────────────────────────────────────────

    async def find_job_links(self, careers_url: str, company_name: str) -> list[dict]:
        """Extract individual job posting links from a careers page."""
        page = await self.browser.new_page()
        jobs = []
        try:
            if not await self.browser.navigate(page, careers_url):
                return jobs

            links = await page.query_selector_all("a[href]")
            seen_hrefs = set()

            for link in links:
                try:
                    href = await link.get_attribute("href") or ""
                    text = (await link.inner_text()).strip()

                    if not text or len(text) < 3 or len(text) > 150:
                        continue

                    full_url = urljoin(careers_url, href)

                    if full_url in seen_hrefs or full_url == careers_url:
                        continue

                    if self.is_blocked_url(full_url):
                        continue

                    is_job_link = any(kw in href.lower() for kw in
                        ["/job/", "/jobs/", "/position/", "/opening/", "/apply/",
                         "greenhouse.io", "comeet.com", "lever.co"])

                    if is_job_link or self._text_matches_role(text):
                        seen_hrefs.add(full_url)
                        ats = self.detect_ats(full_url)
                        jobs.append({
                            "title": text,
                            "url": full_url,
                            "company": company_name,
                            "description": "",
                            "ats_type": ats,
                            "application_method": f"ats:{ats}" if ats else "web_form",
                        })
                except Exception:
                    continue
        finally:
            await page.close()
        return jobs

    def _text_matches_role(self, text: str) -> bool:
        lower = text.lower()
        role_hints = [
            "analyst", "engineer", "specialist", "administrator", "admin",
            "developer", "security", "soc", "devops", "secops", "support",
            "helpdesk", "linux", "network", "monitoring", "pentester",
            "cyber", "it ", "noc", "אבטחת", "מנהל", "תמיכה", "דרוש",
        ]
        return any(hint in lower for hint in role_hints)

    # ── Match jobs to target roles (with dedup) ────────────────────────────

    def match_jobs(self, raw_jobs: list[dict]) -> list[dict]:
        matched = []
        seen_roles = set()  # within-batch dedup: company+role

        for job in raw_jobs:
            if self.is_blocked_url(job.get("url", "")):
                continue

            combined = (job["title"] + " " + job.get("description", "")).lower()
            for role in self.target_roles:
                if any(kw.lower() in combined for kw in role["keywords"]):
                    company = job.get("company", "")
                    dedup_key = f"{company.lower()}::{role['title'].lower()}"

                    if dedup_key in seen_roles:
                        break  # skip duplicate company+role

                    seen_roles.add(dedup_key)
                    matched.append({
                        **job,
                        "matched_role": role["title"],
                        "cv_file": role["cv"],
                    })
                    break
        return matched

    # ── Find HR email ──────────────────────────────────────────────────────

    async def find_hr_email(self, company: dict) -> Optional[str]:
        base_url = company.get("url")
        if not base_url:
            return None
        if not base_url.startswith("http"):
            base_url = "https://" + base_url

        page = await self.browser.new_page()
        try:
            for path in [
                company.get("careers_url", ""),
                urljoin(base_url, "/contact"),
                urljoin(base_url, "/about"),
                base_url,
            ]:
                if not path:
                    continue
                if await self.browser.navigate(page, path, timeout=8000):
                    text = await self.browser.safe_get_text(page)
                    emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', text)
                    for email in emails:
                        lower = email.lower()
                        if any(kw in lower for kw in
                               ["hr@", "jobs@", "careers@", "recruit",
                                "hiring@", "talent@", "apply@", "cv@", "resume@"]):
                            return email
                    domain = urlparse(base_url).netloc.replace("www.", "")
                    for email in emails:
                        if domain in email.lower():
                            return email
        except Exception as e:
            logger.warning("Email search error for %s: %s", company.get("name"), e)
        finally:
            await page.close()
        return None

    @staticmethod
    def construct_email_candidates(company_url: str) -> list[str]:
        if not company_url:
            return []
        domain = urlparse(company_url if "://" in company_url else f"https://{company_url}").netloc
        domain = domain.replace("www.", "")
        if not domain:
            return []
        return [f"jobs@{domain}", f"careers@{domain}", f"hr@{domain}"]
