"""User profile extraction utilities.

Single source of truth:
- Prefer parsing `life-story.md` (human-authored, stable).
- Fall back to `profile.yaml` for any missing fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

_PROJECT_ROOT             = Path(__file__).parent
DEFAULT_LIFE_STORY_PATH   = _PROJECT_ROOT / "life-story.md"
DEFAULT_PROFILE_YAML_PATH = _PROJECT_ROOT / "profile.yaml"


@dataclass(frozen=True)
class Person:
    full_name: str = ""
    email:     str = ""
    phone:     str = ""
    location:  str = ""
    linkedin:  str = ""
    github:    str = ""
    website:   str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _first_nonempty(*values: str) -> str:
    for v in values:
        v = (v or "").strip()
        if v:
            return v
    return ""


def _parse_life_story(text: str) -> Person:
    """Extract contact fields from life-story.md.

    Expects lines like:
        - **Email:** someone@example.com
        - **LinkedIn:** https://linkedin.com/in/handle
    """
    def get_field(label: str) -> str:
        pattern = rf"^\s*-\s*\*\*{re.escape(label)}:\*\*\s*(.+?)\s*$"
        m = re.search(pattern, text, flags=re.MULTILINE | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    full_name = get_field("Full Name")
    email     = get_field("Email")
    phone     = get_field("Phone")
    location  = get_field("Location")
    linkedin  = get_field("LinkedIn")
    github    = get_field("GitHub")
    website   = get_field("Website")

    if not phone:
        m = re.search(r"(\+\d{1,3}\s?\d[\d\s\-]{7,}\d)", text)
        if m:
            phone = m.group(1).strip()

    return Person(
        full_name=full_name,
        email=email,
        phone=phone,
        location=location,
        linkedin=linkedin,
        github=github,
        website=website,
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _load_profile_yaml(path: Path) -> dict:
    """Load contact fields from profile.yaml using PyYAML."""
    if not path.exists():
        return {}
    text = _read_text(path)
    if not text:
        return {}

    try:
        import yaml
        data = yaml.safe_load(text) or {}
    except Exception:
        return {}

    pipeline = data.get("pipeline", {}) or {}

    return {
        "name":     str(data.get("name", "") or "").strip(),
        "email":    str(
            pipeline.get("email_recipient", "")
            or data.get("email", "")
            or ""
        ).strip(),
        "phone":    str(data.get("phone", "") or "").strip(),
        "location": str(data.get("location", "") or "").strip(),
        "linkedin": str(data.get("linkedin", "") or "").strip(),
        "github":   str(data.get("github", "") or "").strip(),
        "website":  str(data.get("website", "") or "").strip(),
    }


def load_person(
    *,
    life_story_path: Path = DEFAULT_LIFE_STORY_PATH,
    profile_yaml_path: Path = DEFAULT_PROFILE_YAML_PATH,
) -> Person:
    """Load user identity and contact info.

    life-story.md takes priority over profile.yaml for every field.
    """
    life_story = _read_text(life_story_path)
    from_life  = _parse_life_story(life_story) if life_story else Person()

    profile      = _load_profile_yaml(profile_yaml_path)
    from_profile = Person(
        full_name=profile.get("name", ""),
        email    =profile.get("email", ""),
        phone    =profile.get("phone", ""),
        location =profile.get("location", ""),
        linkedin =profile.get("linkedin", ""),
        github   =profile.get("github", ""),
        website  =profile.get("website", ""),
    )

    return Person(
        full_name=_first_nonempty(from_life.full_name, from_profile.full_name),
        email    =_first_nonempty(from_life.email,     from_profile.email),
        phone    =_first_nonempty(from_life.phone,     from_profile.phone),
        location =_first_nonempty(from_life.location,  from_profile.location),
        linkedin =_first_nonempty(from_life.linkedin,  from_profile.linkedin),
        github   =_first_nonempty(from_life.github,    from_profile.github),
        website  =_first_nonempty(from_life.website,   from_profile.website),
    )


def split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into first and last name."""
    parts = [p for p in (full_name or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])