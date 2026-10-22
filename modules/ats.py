"""
ATS job feeds.

Most careers pages render their listings with JavaScript or an embedded ATS
widget, so scraping <a> tags off them finds section headings, not postings.
The ATS vendors all publish the same listings as JSON. This module spots which
ATS a careers page uses and reads the postings straight from its API.

Detection reads the careers page HTML (and URL). A seed entry in config.json
can pin the answer with an "ats" block when detection misses:

    {"type": "greenhouse", "slug": "torq"}
    {"type": "lever",      "slug": "logz"}
    {"type": "ashby",      "slug": "pointfive"}
    {"type": "comeet",     "uid": "33.00D", "token": "33D1..."}   # careers API
    {"type": "comeet",     "uid": "17.008", "slug": "cyera"}      # hosted page
    {"type": "workday",    "host": "paloaltonetworks.wd5.myworkdayjobs.com",
                           "tenant": "paloaltonetworks", "site": "panwexternalcareers"}

Only postings in Israel, or remote with no other country attached, are kept.
"""

import json
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_PAGES = 5

# ── Detection ─────────────────────────────────────────────────────────────────

_COMEET_API = re.compile(
    r'comeet\.co/careers-api/2\.0/company/([0-9A-Fa-f]{2}\.[0-9A-Fa-f]{3})/positions\?token=([0-9A-Fa-f]+)')
_COMEET_INIT = re.compile(r'COMEET\.init\(\s*\{(.*?)\}\s*\)', re.S)
_COMEET_INIT_TOKEN = re.compile(r'["\']token["\']\s*:\s*["\']([0-9A-Fa-f]{16,})["\']')
_COMEET_INIT_UID = re.compile(r'["\']company-uid["\']\s*:\s*["\']([0-9A-Fa-f]{2}\.[0-9A-Fa-f]{3})["\']')
_COMEET_HOSTED = re.compile(r'comeet\.com/jobs/([A-Za-z0-9_-]+)/([0-9A-Fa-f]{2}\.[0-9A-Fa-f]{3})')
_GREENHOUSE = re.compile(
    r'(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)'
    r'|boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)')
_LEVER = re.compile(r'jobs\.lever\.co/([A-Za-z0-9_-]+)')
_ASHBY = re.compile(r'jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)')
_WORKDAY = re.compile(
    r'https?://(([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)')

_GREENHOUSE_NOT_SLUGS = {"embed", "js", "v1"}


def detect(html: str, url: str = "") -> Optional[dict]:
    """Return an ats block for the first ATS found in the page, or None."""
    text = (url or "") + "\n" + (html or "")

    m = _COMEET_API.search(text)
    if m:
        return {"type": "comeet", "uid": m.group(1), "token": m.group(2)}
    # Read the token from inside COMEET.init: pages often carry other "token" keys.
    for block in _COMEET_INIT.findall(text):
        token, uid = _COMEET_INIT_TOKEN.search(block), _COMEET_INIT_UID.search(block)
        if token and uid:
            return {"type": "comeet", "uid": uid.group(1), "token": token.group(1)}
    m = _COMEET_HOSTED.search(text)
    if m:
        return {"type": "comeet", "slug": m.group(1), "uid": m.group(2)}

    for m in _GREENHOUSE.finditer(text):
        slug = m.group(1) or m.group(2)
        if slug and slug.lower() not in _GREENHOUSE_NOT_SLUGS:
            return {"type": "greenhouse", "slug": slug}

    m = _LEVER.search(text)
    if m:
        return {"type": "lever", "slug": m.group(1)}

    m = _ASHBY.search(text)
    if m:
        return {"type": "ashby", "slug": m.group(1)}

    m = _WORKDAY.search(text)
    if m:
        return {"type": "workday", "host": m.group(1), "tenant": m.group(2), "site": m.group(3)}

    return None


# ── Location filter ───────────────────────────────────────────────────────────

_ISRAEL_MARKERS = (
    "airport city", "bat yam", "be'er sheva", "beer sheva", "beersheba", "bnei brak",
    "caesarea", "givat shmuel", "givatayim", "haifa", "herzelia", "herzliya",
    "hod hasharon", "holon", "israel", "jerusalem", "kfar saba", "kiryat", "lod",
    "migdal haemek", "modi'in", "modiin", "nes ziona", "ness ziona", "netanya", "or yehuda",
    "petach tikva", "petah tikva", "petah tiqva", "ra'anana", "raanana", "ramat gan",
    "ramla", "rehovot", "rishon", "rosh ha'ayin", "rosh haayin", "tel aviv", "tel-aviv",
    "yokneam", "yoqneam",
    "אור יהודה", "הרצליה", "חולון", "חיפה", "ירושלים", "ישראל", "נתניה", "פתח תקווה",
    "ראשון לציון", "רחובות", "רמת גן", "רעננה", "תל אביב",
)
_COUNTRYLESS_REMOTE = ("remote", "anywhere", "worldwide", "global", "emea")


def _israel(text: str) -> bool:
    lower = text.lower()
    return any(re.search(rf"(?<!\w){re.escape(m)}(?!\w)", lower) for m in _ISRAEL_MARKERS)


def location_ok(location: str, country: Optional[str] = None, is_remote: bool = False) -> bool:
    """
    Israel, or remote with nothing tying it to another country.

    A structured country code wins when present. Otherwise the location text
    decides: 'Remote' or 'Remote - EMEA' pass, 'Remote - US' does not.
    """
    location = location or ""
    if country:
        return country.upper() in ("IL", "ISR", "ISRAEL")
    if _israel(location):
        return True
    words = [w for w in re.split(r"[\s,;/()|-]+", location.lower()) if w]
    if not words:
        return is_remote
    return (is_remote or "remote" in words) and all(w in _COUNTRYLESS_REMOTE for w in words)


# ── Fetchers ──────────────────────────────────────────────────────────────────
# Each returns (title, url, location_text, country_or_None, is_remote) tuples.

async def _comeet(client: httpx.AsyncClient, ats: dict) -> list[tuple]:
    if ats.get("token"):
        resp = await client.get(
            f"https://www.comeet.co/careers-api/2.0/company/{ats['uid']}/positions",
            params={"token": ats["token"]})
        resp.raise_for_status()
        positions = resp.json()
    else:
        resp = await client.get(f"https://www.comeet.com/jobs/{ats['slug']}/{ats['uid']}")
        resp.raise_for_status()
        m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);\s*\n", resp.text, re.S)
        positions = json.loads(m.group(1)) if m else []

    out = []
    for p in positions:
        loc = p.get("location") or {}
        url = p.get("url_comeet_hosted_page") or p.get("url_active_page") or ""
        # Comeet sets is_remote on hybrid office roles too, so country decides.
        out.append((p.get("name", ""), url,
                    ", ".join(x for x in (loc.get("city"), loc.get("name")) if x),
                    loc.get("country"), bool(loc.get("is_remote"))))
    return out


async def _greenhouse(client: httpx.AsyncClient, ats: dict) -> list[tuple]:
    resp = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{ats['slug']}/jobs")
    resp.raise_for_status()
    return [(j.get("title", ""), j.get("absolute_url", ""),
             (j.get("location") or {}).get("name", ""), None, False)
            for j in resp.json().get("jobs", [])]


async def _lever(client: httpx.AsyncClient, ats: dict) -> list[tuple]:
    resp = await client.get(f"https://api.lever.co/v0/postings/{ats['slug']}", params={"mode": "json"})
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):          # {"ok": false, ...} for a dead slug
        return []
    return [(j.get("text", ""), j.get("hostedUrl", ""),
             (j.get("categories") or {}).get("location", ""),
             j.get("country"), (j.get("workplaceType") or "").lower() == "remote")
            for j in data]


async def _ashby(client: httpx.AsyncClient, ats: dict) -> list[tuple]:
    resp = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{ats['slug']}")
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        out.append((j.get("title", ""), j.get("jobUrl", ""),
                    ", ".join(x for x in (j.get("location"), addr.get("addressCountry")) if x),
                    None, bool(j.get("isRemote"))))
    return out


async def _workday(client: httpx.AsyncClient, ats: dict) -> list[tuple]:
    # Workday tenants list every country; searching "Israel" narrows it server-side.
    host, tenant, site = ats["host"], ats["tenant"], ats["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out = []
    for page in range(WORKDAY_MAX_PAGES):
        resp = await client.post(api, json={
            "appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE,
            "offset": page * WORKDAY_PAGE_SIZE, "searchText": "Israel"})
        resp.raise_for_status()
        data = resp.json()
        postings = data.get("jobPostings", [])
        for j in postings:
            loc = j.get("locationsText", "")
            # "2 Locations" hides the cities; the Israel search already scoped it.
            if re.fullmatch(r"\d+ Locations", loc):
                loc = "Israel"
            out.append((j.get("title", ""), f"https://{host}/{site}{j.get('externalPath', '')}",
                        loc, None, False))
        if len(postings) < WORKDAY_PAGE_SIZE or len(out) >= data.get("total", 0):
            break
    return out


_FETCHERS = {
    "comeet": _comeet,
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workday": _workday,
}


async def fetch_jobs(ats: dict, company_name: str, careers_url: str) -> Optional[list[dict]]:
    """
    Postings from the ATS, filtered to Israel/remote, in find_job_links' shape.

    Returns None when the feed could not be read, so the caller can fall back
    to scraping. An empty list means the feed was read and had nothing local.
    """
    fetcher = _FETCHERS.get(ats.get("type", ""))
    if not fetcher:
        return None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as client:
            rows = await fetcher(client, ats)
    except Exception as e:
        logger.warning("ATS feed failed for %s (%s): %s", company_name, ats.get("type"), e)
        return None

    jobs = []
    dropped = 0
    for title, url, location, country, is_remote in rows:
        if not title or not url:
            continue
        if not location_ok(location, country, is_remote):
            dropped += 1
            continue
        jobs.append({
            "title": title.strip(),
            "url": url,
            "company": company_name,
            "careers_url": careers_url,
            "description": "",
            "location": location,
            "ats_type": ats["type"],
            "application_method": f"ats:{ats['type']}",
        })
    logger.info("ATS %s for %s: %d posting(s), %d outside Israel/remote dropped",
                ats["type"], company_name, len(jobs), dropped)
    return jobs
