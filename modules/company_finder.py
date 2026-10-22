import logging
import re
import httpx
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Words that indicate a search result is an article/list, not a company
ARTICLE_TITLE_PATTERNS = [
    r'^\d+\s+(top|best|leading|prominent|biggest)',
    r'(startups|companies)\s+(in|to watch|you should)',
    r'(list of|guide to|overview of|ranking)',
    r'(top \d+|best \d+|\d+ best)',
    r'(unveiling|exploring|discovering|roundup)',
    r'\b(blog|article|report|review|news)\b',
    r'\b\d{4}\b.*\b(startups|companies)\b',  # "2026 ... companies"
]

# Job boards and aggregators list other companies' jobs; they are not employers.
# A discovered "company" on one of these domains, or named like a listing page
# ("Cyber Security Jobs in Tel Aviv"), is dropped.
JOB_BOARD_DOMAINS = {
    "cyberlist.co", "linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.co.il",
    "alljobs.co.il", "drushim.co.il", "jobmaster.co.il", "jobnet.co.il", "jobkarov.com",
    "gotfriends.co.il", "secrettelaviv.com", "builtin.com", "wellfound.com", "angel.co",
    "startup.jobs", "ziprecruiter.com", "monster.com", "simplyhired.com", "techaviv.com",
    "geektime.co.il", "finder.startupnationcentral.org", "startupnationcentral.org",
    "comeet.com", "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "cybersecurityjobs.com", "cyberseek.org", "infosec-jobs.com", "remoteok.com",
    "weworkremotely.com", "dice.com", "jooble.org", "careerjet.co.il", "jobs.co.il",
}
JOB_BOARD_NAME_PATTERN = re.compile(
    r'\b(jobs?|careers?|vacanc(y|ies)|openings|hiring|recruit(ing|ment)?)\b'
    r'|דרושים|משרות|דרושות',
    re.IGNORECASE,
)

# Minimum requirements for a valid company name
COMPANY_NAME_MIN_LENGTH = 2
COMPANY_NAME_MAX_LENGTH = 50


class CompanyFinder:
    def __init__(self, config: dict):
        self.config = config
        self.seed_list = config.get("company_seed_list", [])
        self.search_areas = config["geography"]["search_areas"]
        self.excluded = [n.lower().strip() for n in config.get("excluded_companies", [])]

    def is_excluded(self, name: str) -> bool:
        """True if the company name contains any config.json excluded_companies entry."""
        name = name.lower().strip()
        return any(ex in name for ex in self.excluded)

    def get_seed_companies(self) -> list[dict]:
        return [
            {
                "name": c["name"],
                "url": c["url"],
                "location": c["location"],
                "source": "seed_list",
                "status": "pending",
                # Optional per-company overrides (see modules/ats.py)
                **{k: c[k] for k in ("ats", "careers_url") if k in c},
            }
            for c in self.seed_list
        ]

    async def search_additional_companies(self) -> list[dict]:
        """
        Uses DuckDuckGo to discover companies in Israeli high-tech hubs.
        Filters out article titles and list pages.
        """
        found = {}

        # More targeted queries that return company names, not listicles
        queries = []
        for area in self.search_areas[:6]:
            queries.extend([
                f'cybersecurity company "{area}" Israel hiring',
                f'IT services company "{area}" Israel careers',
                f'SOC managed security "{area}" Israel',
            ])

        # Deduplicate queries
        queries = list(dict.fromkeys(queries))[:15]

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for query in queries:
                try:
                    resp = await client.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": query},
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"},
                    )

                    # Extract result titles and URLs
                    titles = re.findall(r'<a[^>]+class="result__a"[^>]*>([^<]+)</a>', resp.text)
                    urls = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', resp.text)

                    for i, title in enumerate(titles[:5]):
                        name = self._extract_company_name(title)
                        if not name:
                            continue
                        if self._is_article_title(title):
                            continue
                        if name.lower() in found:
                            continue

                        # Try to get URL
                        url = urls[i] if i < len(urls) else None
                        if url:
                            url = self._clean_url(url)

                        if self._is_job_board(name, url):
                            logger.info("Skipping job board: %s (%s)", name, url)
                            continue

                        # Extract area from query
                        area = "Israel"
                        for a in self.search_areas:
                            if a.lower() in query.lower():
                                area = a
                                break

                        found[name.lower()] = {
                            "name": name,
                            "url": url,
                            "location": area,
                            "source": "web_search",
                            "status": "pending",
                        }

                except Exception as e:
                    logger.warning("Web search failed for query '%s': %s", query[:50], e)

        return list(found.values())

    def _extract_company_name(self, title: str) -> Optional[str]:
        """Extract a likely company name from a search result title."""
        # Common separators in search results
        for sep in [" - ", " | ", " — ", " · ", " :: "]:
            if sep in title:
                name = title.split(sep)[0].strip()
                if self._is_valid_company_name(name):
                    return name

        # If no separator, use the full title if it looks like a name
        name = title.strip()
        if self._is_valid_company_name(name):
            return name

        return None

    def _is_valid_company_name(self, name: str) -> bool:
        """Check if a string looks like a company name, not an article title."""
        if not name:
            return False
        if len(name) < COMPANY_NAME_MIN_LENGTH or len(name) > COMPANY_NAME_MAX_LENGTH:
            return False
        if self._is_article_title(name):
            return False
        # Reject if it's mostly numbers
        if sum(c.isdigit() for c in name) > len(name) // 2:
            return False
        return True

    def _is_article_title(self, text: str) -> bool:
        """Check if text looks like an article/listicle title rather than a company name."""
        lower = text.lower().strip()
        for pattern in ARTICLE_TITLE_PATTERNS:
            if re.search(pattern, lower):
                return True
        # Long titles (>50 chars) are almost always articles
        if len(text) > COMPANY_NAME_MAX_LENGTH:
            return True
        return False

    def _is_job_board(self, name: str, url: Optional[str]) -> bool:
        """True for aggregators and listing pages that would pose as employers."""
        if JOB_BOARD_NAME_PATTERN.search(name or ""):
            return True
        if url:
            domain = urlparse(url).netloc.lower().removeprefix("www.")
            if any(domain == d or domain.endswith("." + d) for d in JOB_BOARD_DOMAINS):
                return True
        return False

    def _clean_url(self, url: str) -> Optional[str]:
        """Extract the actual domain URL from a DuckDuckGo redirect."""
        if not url:
            return None
        # DuckDuckGo sometimes wraps URLs
        if "uddg=" in url:
            import urllib.parse
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "uddg" in parsed:
                url = parsed["uddg"][0]
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return None

    def merge_and_deduplicate(self, seed: list[dict], discovered: list[dict]) -> list[dict]:
        seen = set()
        result = []

        # Seed companies have priority
        for company in seed + discovered:
            key = company["name"].lower().strip()
            # Also check domain dedup
            domain_key = None
            if company.get("url"):
                domain_key = urlparse(company["url"]).netloc.lower().replace("www.", "")

            if key in seen or (domain_key and domain_key in seen):
                continue
            if self.is_excluded(company["name"]):
                logger.info("Skipping excluded company: %s", company["name"])
                continue

            seen.add(key)
            if domain_key:
                seen.add(domain_key)
            result.append(company)

        return result

    def prioritize(self, companies: list[dict]) -> list[dict]:
        # Proximity boost driven by config: geography.search_areas, plus the base town.
        geo = self.config.get("geography", {})
        nearby = {a.lower() for a in geo.get("search_areas", [])}
        if geo.get("base"):
            nearby.add(geo["base"].split(",")[0].strip().lower())

        def priority_key(c):
            loc = c.get("location", "").lower()
            is_nearby = any(n in loc for n in nearby)
            is_seed = c.get("source") == "seed_list"
            return (0 if is_seed else 1, 0 if is_nearby else 1, c["name"])

        return sorted(companies, key=priority_key)
