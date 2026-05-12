"""
job_scraper.py
Scrapes jobs from local government, public agencies, and nonprofits.
Sources: USAJobs, GovernmentJobs (NEOGOV), Idealist, Seattle Times Jobs

Install dependencies:
    pip install requests beautifulsoup4 lxml

Usage:
    python job_scraper.py --title "director" --location "seattle"
"""

import requests
import json
import time
import argparse
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlencode, quote_plus

# ── Shared HTTP session ────────────────────────────────────────────────────────
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
})

# ── Normalised job schema ──────────────────────────────────────────────────────
def make_job(title, org, location, url, source, posted=None, closing=None):
    return {
        "title":    title,
        "org":      org,
        "location": location,
        "url":      url,
        "source":   source,
        "posted":   posted,
        "closing":  closing,
        "scraped":  datetime.now().isoformat(timespec="seconds"),
    }


# ── 1. USAJobs (federal) ───────────────────────────────────────────────────────
# Free API key at: https://developer.usajobs.gov/
USAJOBS_KEY = "VhoauaHXpWY5nqgFTfXJBLOssWN8zWrh4Fdrg9GDOQg="  # sign up at developer.usajobs.gov
USAJOBS_EMAIL = "rebecca@lbs.ventures"

def search_usajobs(title: str, location: str = "Washington") -> list[dict]:
    url = "https://data.usajobs.gov/api/search"
    params = {"Keyword": title, "LocationName": location, "ResultsPerPage": 50}
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": USAJOBS_EMAIL,
        "Authorization-Key": USAJOBS_KEY,
    }
    try:
        r = session.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        items = r.json()["SearchResult"]["SearchResultItems"]
        jobs = []
        for item in items:
            p = item["MatchedObjectDescriptor"]
            jobs.append(make_job(
                title    = p.get("PositionTitle"),
                org      = p.get("OrganizationName"),
                location = p.get("PositionLocationDisplay"),
                url      = p.get("PositionURI"),
                source   = "USAJobs",
                posted   = p.get("PublicationStartDate", "")[:10],
                closing  = p.get("ApplicationCloseDate", "")[:10],
            ))
        return jobs
    except Exception as e:
        print(f"[USAJobs] Error: {e}")
        return []


# ── 2. GovernmentJobs / NEOGOV ─────────────────────────────────────────────────
# Covers: WA State, King County, Seattle, and the general keyword search

NEOGOV_SOURCES = [
    # (label, base_search_url)
    ("GovernmentJobs-WA",    "https://www.governmentjobs.com/jobs"),
    ("GovernmentJobs-King",  "https://www.governmentjobs.com/careers/kingcounty"),
    ("GovernmentJobs-SEA",   "https://www.governmentjobs.com/careers/seattle"),
    ("GovernmentJobs-WASt",  "https://www.governmentjobs.com/careers/washington"),
]

def _scrape_neogov_page(url: str, label: str) -> list[dict]:
    """Scrape a single NEOGOV listing page."""
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        jobs = []
        # NEOGOV renders job cards with class="views-row" or inside <table id="job-table">
        rows = soup.select("tr.views-row, div.views-row, .job-listing")
        if not rows:
            # Fallback: grab all <a> tags inside the jobs table
            rows = soup.select("#job-table tbody tr")

        for row in rows:
            link = row.select_one("a[href*='/careers/'], a[href*='/jobs/']")
            if not link:
                link = row.select_one("a")
            if not link:
                continue

            title = link.get_text(strip=True)
            href  = link.get("href", "")
            if href.startswith("/"):
                href = "https://www.governmentjobs.com" + href

            # Department / org
            org_el = row.select_one(".department, .employer, td:nth-child(2)")
            org = org_el.get_text(strip=True) if org_el else label

            # Location
            loc_el = row.select_one(".location, td:nth-child(3)")
            loc = loc_el.get_text(strip=True) if loc_el else ""

            # Closing date
            close_el = row.select_one(".closing-date, td:nth-child(4)")
            closing = close_el.get_text(strip=True) if close_el else None

            if title:
                jobs.append(make_job(title, org, loc, href, label, closing=closing))

        return jobs
    except Exception as e:
        print(f"[{label}] Error scraping {url}: {e}")
        return []


def search_neogov(title: str, location: str = "Seattle, WA") -> list[dict]:
    all_jobs = []
    for label, base_url in NEOGOV_SOURCES:
        # Build search URL with keyword filter
        if "careers/" in base_url:
            # Agency-specific portal: keyword goes in query param
            url = f"{base_url}?search=true&keyword={quote_plus(title)}"
        else:
            url = f"{base_url}?keyword={quote_plus(title)}&location={quote_plus(location)}"

        print(f"  Scraping {label}...")
        jobs = _scrape_neogov_page(url, label)
        print(f"    → {len(jobs)} jobs found")
        all_jobs.extend(jobs)
        time.sleep(1.5)  # be polite

    return all_jobs


# ── 3. Idealist (nonprofits) ───────────────────────────────────────────────────
def search_idealist(title: str, location: str = "Seattle, WA") -> list[dict]:
    """
    Idealist has a public search page. We hit the JSON endpoint their
    React frontend uses (no auth required, but may change).
    """
    url = "https://www.idealist.org/api/v1/listings"
    params = {
        "q":                  title,
        "location":           location,
        "type":               "JOB",
        "professionalLevel":  ["EXECUTIVE", "DIRECTOR"],
        "page":               1,
        "pageSize":           50,
    }
    try:
        r = session.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        jobs = []
        for item in data.get("results", []):
            jobs.append(make_job(
                title    = item.get("title"),
                org      = item.get("org", {}).get("name"),
                location = item.get("locationType") or item.get("location", ""),
                url      = "https://www.idealist.org/en/job/" + item.get("id", ""),
                source   = "Idealist",
                posted   = item.get("publishedAt", "")[:10],
                closing  = item.get("applicationDeadline", "")[:10] or None,
            ))
        return jobs
    except Exception as e:
        # Fallback: scrape the HTML search page
        print(f"[Idealist] JSON API failed ({e}), trying HTML scrape...")
        return _scrape_idealist_html(title, location)


def _scrape_idealist_html(title: str, location: str) -> list[dict]:
    url = (
        f"https://www.idealist.org/en/jobs"
        f"?q={quote_plus(title)}"
        f"&location={quote_plus(location)}"
        f"&professionalLevel=EXECUTIVE&professionalLevel=DIRECTOR"
    )
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        jobs = []
        for card in soup.select("[data-testid='listing-card'], .listing-card, article"):
            title_el = card.select_one("h2, h3, [data-testid='listing-title']")
            org_el   = card.select_one("[data-testid='org-name'], .org-name")
            loc_el   = card.select_one("[data-testid='location'], .location")
            link_el  = card.select_one("a[href]")
            if not title_el:
                continue
            href = link_el["href"] if link_el else ""
            if href.startswith("/"):
                href = "https://www.idealist.org" + href
            jobs.append(make_job(
                title    = title_el.get_text(strip=True),
                org      = org_el.get_text(strip=True) if org_el else "",
                location = loc_el.get_text(strip=True) if loc_el else "",
                url      = href,
                source   = "Idealist",
            ))
        return jobs
    except Exception as e:
        print(f"[Idealist] HTML scrape failed: {e}")
        return []


# ── 4. Seattle Times Jobs ──────────────────────────────────────────────────────
def search_seattletimes_jobs(title: str, location: str = "Seattle, WA") -> list[dict]:
    url = f"https://jobs.seattletimes.com/search?keyword={quote_plus(title)}&location={quote_plus(location)}"
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        jobs = []
        for card in soup.select(".job-listing, .listing-item, article.job"):
            title_el = card.select_one("h2 a, h3 a, .job-title a")
            org_el   = card.select_one(".company, .employer, .organization")
            loc_el   = card.select_one(".location, .city")
            if not title_el:
                continue
            href = title_el.get("href", "")
            if href.startswith("/"):
                href = "https://jobs.seattletimes.com" + href
            jobs.append(make_job(
                title    = title_el.get_text(strip=True),
                org      = org_el.get_text(strip=True) if org_el else "",
                location = loc_el.get_text(strip=True) if loc_el else location,
                url      = href,
                source   = "SeattleTimes",
            ))
        return jobs
    except Exception as e:
        print(f"[SeattleTimes] Error: {e}")
        return []


# ── Main runner ────────────────────────────────────────────────────────────────
def scrape_all(title: str, location: str = "Seattle, WA") -> list[dict]:
    print(f"\nSearching for: '{title}' near {location}\n")
    all_jobs = []

    print("[1/4] USAJobs (federal)...")
    jobs = search_usajobs(title, location)
    print(f"  → {len(jobs)} jobs")
    all_jobs.extend(jobs)

    print("[2/4] GovernmentJobs / NEOGOV...")
    jobs = search_neogov(title, location)
    print(f"  → {len(jobs)} jobs total")
    all_jobs.extend(jobs)

    print("[3/4] Idealist (nonprofits)...")
    jobs = search_idealist(title, location)
    print(f"  → {len(jobs)} jobs")
    all_jobs.extend(jobs)

    print("[4/4] Seattle Times Jobs...")
    jobs = search_seattletimes_jobs(title, location)
    print(f"  → {len(jobs)} jobs")
    all_jobs.extend(jobs)

    # Deduplicate by URL
    seen = set()
    unique = []
    for job in all_jobs:
        if job["url"] not in seen:
            seen.add(job["url"])
            unique.append(job)

    print(f"\nTotal unique jobs found: {len(unique)}")
    return unique


def save_results(jobs: list[dict], filename: str = "jobs.json"):
    with open(filename, "w") as f:
        json.dump(jobs, f, indent=2)
    print(f"Saved to {filename}")

    # Also print a quick summary table
    print(f"\n{'TITLE':<50} {'ORG':<35} {'SOURCE':<20}")
    print("-" * 110)
    for j in jobs:
        print(f"{str(j['title']):<50} {str(j['org']):<35} {str(j['source']):<20}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape local gov & nonprofit jobs")
    parser.add_argument("--title",    default="director", help="Job title keyword")
    parser.add_argument("--location", default="Seattle, WA", help="Location")
    parser.add_argument("--output",   default="jobs.json", help="Output JSON file")
    args = parser.parse_args()

    jobs = scrape_all(args.title, args.location)
    save_results(jobs, args.output)