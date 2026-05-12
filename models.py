"""Data models for the job application pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List


class JobBoard(Enum):
    # General boards
    INDEED      = "indeed"
    LINKEDIN    = "linkedin"
    GLASSDOOR   = "glassdoor"
    GOOGLE      = "google"

    # ATS platforms (company-specific)
    GREENHOUSE  = "greenhouse"
    LEVER       = "lever"

    # Government & public sector
    USAJOBS         = "usajobs"          # Federal jobs via official API
    GOVERNMENTJOBS  = "governmentjobs"  # WA State, King County, Seattle (NEOGOV)

    # Nonprofit & mission-driven
    IDEALIST    = "idealist"

    # Remote-focused
    REMOTIVE    = "remotive"
    HIMALAYAS   = "himalayas"

    # API-based aggregators
    JSEARCH     = "jsearch"     # Google Jobs via RapidAPI
    ADZUNA      = "adzuna"      # Needs ADZUNA_APP_ID + ADZUNA_APP_KEY

    # Other
    ARBEITNOW       = "arbeitnow"
    THEMUSE         = "themuse"
    STEPSTONE       = "stepstone"
    LINKEDIN_POSTS  = "linkedin_posts"
    INTERNET        = "internet"

    # MENA / international (not used by default — commented out in profile.yaml)
    BAYT        = "bayt"
    GULFTALENT  = "gulftalent"
    WUZZUF      = "wuzzuf"


@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    board: JobBoard
    description: str = ""
    salary: str = ""
    date_posted: str = ""
    job_type: str = ""  # full-time, part-time, contract
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat())
    match_score: float = 0.0
    match_details: Dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Unique identifier based on URL."""
        return self.url

    def to_dict(self) -> dict:
        return {
            "title":        self.title,
            "company":      self.company,
            "location":     self.location,
            "url":          self.url,
            "board":        self.board.value,
            "description":  self.description,
            "salary":       self.salary,
            "date_posted":  self.date_posted,
            "job_type":     self.job_type,
            "scraped_at":   self.scraped_at,
            "match_score":  self.match_score,
            "match_details": self.match_details,
        }


@dataclass
class SearchQuery:
    keywords: str
    location: str = ""
    remote: bool = False
    job_type: str = ""  # full-time, part-time, contract
    max_age_days: int = 5
    boards: List[JobBoard] = field(default_factory=lambda: list(JobBoard))