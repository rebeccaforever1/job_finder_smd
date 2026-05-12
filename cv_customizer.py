"""Automated CV customization engine.

Reads life-story.md + job description, generates tailored LaTeX files
(employment.tex, skills.tex, projects.tex), compiles to PDF.
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from llm import generate_latex, generate_structured, check_ollama_available
from user_profile import load_person, split_name

logger = logging.getLogger(__name__)

# Project root — life-story.md lives here by default
_PROJECT_ROOT = Path(__file__).parent

# Default CV directory: ./cv/ inside the project.
# Override via `cv_dir` in profile.yaml (supports ~ expansion and absolute paths).
_DEFAULT_CV_DIR = _PROJECT_ROOT / "cv"

# Stable files to symlink from cv/ into each per-application directory
SYMLINK_FILES = [
    "education.tex", "teaching.tex", "publications.tex", "misc.tex",
    "referee.tex", "referee-full.tex",
    "own-bib.bib", "photo.png", "photo.jpg", "settings.sty",
]

# No few-shot examples needed — prompts are driven entirely by life-story.md
EXAMPLE_EMPLOYMENT: Dict[str, Path] = {}
EXAMPLE_SKILLS: Dict[str, Path] = {}
EXAMPLE_PROJECTS: Dict[str, Path] = {}

# Writing rules applied to all LLM-generated CV content
CV_WRITING_RULES = """
WRITING RULES — follow these exactly:

Sentence and bullet structure:
- Every bullet must be a direct affirmative statement with an actor, action, and measurable object.
- Do not use not-X-but-Y structures. Rewrite as a single affirmative statement.
- Do not negate a concept and restate it positively. State the positive directly.
- Do not use contrast, reversal, or reframing as a structural device.
- If emphasis is needed, add a specific detail or metric — not an opposing clause.
- One complete idea per bullet.

Prohibited constructions:
- No em dashes.
- No rhetorical fragments used for emphasis or suspense.
- No stacked parallel hype phrases (e.g. "Built X. Scaled Y. Delivered Z." as pure cadence).
- No abstract nouns without measurable or observable referents.
- No superlatives like "best," "top," or "ideal" unless defined by a metric.
- No sentence fragments.

Prohibited words and phrases:
- leverage, cadence, touchpoint, anchor, framing, lever, moment, signal,
  belonging, alignment, pipeline (when used metaphorically), synergy,
  spearheaded, championed, revolutionized, transformed (unless tied to a metric)

Style:
- Lead each bullet with a strong past-tense verb tied to an operation or outcome.
- Prefer concrete variables, actions, and metrics over abstract nouns.
- Combine related observations when they describe the same entity or metric.
- Do not produce one bare fact per bullet with no context. Each bullet should
  contain an action and either a scale, metric, or observable outcome.
"""


def ensure_miktex_auto_install() -> None:
    """Best-effort: disable MiKTeX package install popups."""
    try:
        subprocess.run(
            ["initexmf", "--set-config-value=[MPM]AutoInstall=1"],
            capture_output=True, text=True, timeout=20,
        )
        subprocess.run(
            ["initexmf", "--set-config-value=[MPM]AskInstall=0"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        pass


def _is_valid_pdf(path: Path) -> bool:
    """Cheap PDF integrity check: header + EOF marker + minimum size."""
    try:
        if not path.exists() or path.stat().st_size < 10_000:
            return False
        data = path.read_bytes()
        if not data.startswith(b"%PDF"):
            return False
        return b"%%EOF" in data[-2048:]
    except Exception:
        return False


def _latex_escape(text: str) -> str:
    if text is None:
        return ""
    return (
        str(text)
        .replace("\\", r"\textbackslash{}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("~", r"\textasciitilde{}")
        .replace("^", r"\textasciicircum{}")
    )


def personalize_cv_header(app_dir: Path) -> None:
    """Replace YOUR_* placeholders in app_dir/cv-llt.tex using life-story/profile."""
    cv_tex = app_dir / "cv-llt.tex"
    if not cv_tex.exists():
        return

    person = load_person()
    first, last = split_name(person.full_name or "")

    linkedin_handle = (person.linkedin or "").strip()
    if linkedin_handle.startswith("http"):
        linkedin_handle = linkedin_handle.rstrip("/").split("/")[-1]

    github_handle = (person.github or "").strip()
    if github_handle.startswith("http"):
        github_handle = github_handle.rstrip("/").split("/")[-1]

    replacements = {
        "YOUR_FIRST_NAME":              _latex_escape(first or person.full_name),
        "YOUR_LAST_NAME":               _latex_escape(last),
        "YOUR_EMAIL":                   _latex_escape(person.email),
        "YOUR_LINKEDIN_HANDLE":         _latex_escape(linkedin_handle),
        "YOUR_GITHUB":                  _latex_escape(github_handle),
        "YOUR_LASTNAME":                _latex_escape(last),
        "YOUR_FIRSTNAME":               _latex_escape(first),
        "YOUR_MIDDLENAME_OR_INITIAL":   "",
    }

    content = cv_tex.read_text(encoding="utf-8", errors="replace")
    for k, v in replacements.items():
        content = content.replace(k, v)

    has_photo = (app_dir / "photo.png").exists() or (app_dir / "photo.jpg").exists()
    if not has_photo:
        content = content.replace(r"\includecomment{fullonly}", r"\excludecomment{fullonly}")

    cv_tex.write_text(content, encoding="utf-8")


def _looks_like_placeholder(tex: str) -> bool:
    markers = [
        "Your Most Recent Job Title",
        "Ph.D. in YOUR FIELD",
        "YOUR FIELD",
        "Project Name",
        "YOUR_GITHUB/PROJECT",
        "Your domain-specific technical skills here",
    ]
    t = tex or ""
    if any(m in t for m in markers):
        return True
    if re.search(r"\\entry\*\[[^\]]*\]\s*\{", t):
        return True
    return False


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("File not found: %s", path)
        return ""


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text)
    return text[:60]


def _extract_user_name(life_story: str) -> str:
    for line in life_story.splitlines()[:20]:
        m = re.search(r'\*\*Full Name:\*\*\s*(.+)', line)
        if m:
            return m.group(1).strip()
    m = re.search(r'^#\s+Life Story\s*[—–-]\s*(.+)', life_story, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return "the candidate"


def analyze_job(
    description: str,
    title: str = "",
    company: str = "",
    model: str = "qwen3.5:9b",
) -> Dict:
    """Analyze a job description to extract domain, key skills, and keywords.

    """
    prompt = f"""Analyze this job posting and return a JSON object.

Job Title: {title}
Company: {company}

Job Description:
{description[:3000]}

Return JSON with these fields:
-- "domain": one of [
    "service_provider",
    "cloud_networking",
    "enterprise_networking",
    "network_automation",
    "network_security",
    "data_center_networking",
    "government_network",
    "network_education"
  ]
  Choose the domain that best matches the PRIMARY focus of the role.
  Use "service_provider" for ISP, telco, carrier, or SP routing/design roles.
  Use "cloud_networking" for AWS, Azure, GCP, or hyperscale infrastructure roles.
  Use "enterprise_networking" for large enterprise LAN/WAN/campus network roles.
  Use "network_automation" for roles primarily focused on Python/automation/NetDevOps.
  Use "network_security" for roles focused on firewalls, zero trust, or security architecture.
  Use "data_center_networking" for DC fabric, spine-leaf, or colo roles.
  Use "government_network" for public sector, federal, or municipal network roles.
  Use "network_education" for courseware, training, or certification program roles.
  Default to "service_provider" for general senior network engineering roles.

- "key_technologies": list of 5-10 specific technologies or platforms mentioned or implied
- "keywords": list of 5-10 important keywords for this role
- "focus_areas": list of 3-5 main focus areas of the role
- "company_mission": one sentence describing what the company does (if inferrable)
- "seniority": one of ["director", "vp", "head_of", "manager", "individual_contributor"]
- "emphasize_leadership": true if the role requires managing teams or org strategy, false otherwise
"""
    raw = generate_structured(prompt, model=model)
    if isinstance(raw, list) and raw:
        raw = raw[0] if isinstance(raw[0], dict) else {}
    if not isinstance(raw, dict) or not raw:
        raw = {}
    return {
        "domain":               raw.get("domain", "service_provider"),
        "key_technologies":     raw.get("key_technologies", []) or [],
        "keywords":             raw.get("keywords", []) or [],
        "focus_areas":          raw.get("focus_areas", []) or [],
        "company_mission":      raw.get("company_mission", "") or "",
        "seniority":            raw.get("seniority", "individual_contributor") or "individual_contributor",
        "emphasize_leadership": raw.get("emphasize_leadership", True),
    }


def generate_employment_tex(
    life_story: str,
    job_analysis: Dict,
    base_template: str,
    model: str = "qwen3.5:9b",
) -> str:
    """Generate a customized employment.tex for a specific job."""

    user_name = _extract_user_name(life_story)
    domain = job_analysis.get("domain", "service_provider")
    emphasize_leadership = job_analysis.get("emphasize_leadership", False)
    leadership_note = (
        "This role has a leadership or management component. "
        "Highlight team leadership, mentoring, and cross-functional coordination "
        "alongside deep technical expertise."
        if emphasize_leadership else
        "This is a deep technical individual contributor role. "
        "Emphasize protocol expertise, automation work, and hands-on engineering. "
        "Keep management mentions minimal."
    )

    system = f"""You are an expert CV writer helping {user_name} apply for senior network engineering roles.
You produce LaTeX using the curve document class rubric format.
Output ONLY valid LaTeX — no markdown, no explanations, no code fences.
The output must compile with pdflatex without errors.
Do NOT fabricate metrics, technologies, or achievements not in the candidate's background.
Do NOT add company description lines.
Use \\begin{{itemize}}[noitemsep,topsep=2pt,leftmargin=1em] and \\item for bullets.
{CV_WRITING_RULES}"""

    prompt = f"""Customize the employment section of {user_name}'s CV for this specific job.

TARGET JOB:
- Domain: {domain}
- Key Technologies: {', '.join(job_analysis.get('key_technologies', []))}
- Focus Areas: {', '.join(job_analysis.get('focus_areas', []))}
- Keywords: {', '.join(job_analysis.get('keywords', []))}
- Seniority: {job_analysis.get('seniority', 'director')}
- Leadership emphasis: {leadership_note}

{user_name.upper()}'S FULL BACKGROUND (life-story.md):
{life_story[:6000]}

BASE TEMPLATE (current employment.tex — this is the starting point):
{base_template}

RULES:
1. Keep ALL job entries present in the base template. Do not add or remove roles.
2. Keep the exact dates and job titles from the base template.
3. Reorder or reweight bullets in the most recent 2-3 roles to surface experience
   most relevant to the target domain and technologies.
4. For roles older than 5 years, keep bullets concise — 1-2 per role maximum.
5. Use \\textbf{{}} for key metrics and achievements only — not for generic phrases.
6. Use \\begin{{itemize}}[noitemsep,topsep=2pt,leftmargin=1em] and \\item for all bullets.
7. Escape special LaTeX characters: & → \\&, % → \\%, $ → \\$
8. Use the EXACT format: \\begin{{rubric}}{{Experience}} ... \\entry*[dates]% ... \\end{{rubric}}
9. Do NOT add any text outside the rubric environment.
10. Do NOT invent metrics or tools not present in the background above.

Output the complete employment.tex content:"""

    result = generate_latex(prompt, system=system, model=model, max_tokens=3500)

    if "\\begin{rubric}" not in result or "\\end{rubric}" not in result:
        logger.warning(
            "Generated employment.tex is missing rubric structure — falling back to base template. "
            "Check that Ollama is running the 9b model and try again."
        )
        return base_template

    return result


def generate_skills_tex(
    life_story: str,
    job_analysis: Dict,
    base_template: str,
    model: str = "qwen3.5:9b",
) -> str:
    """Generate a customized skills.tex for a specific job.
    
    Sergio's skills categories:
    - Routing Protocols
    - Network Platforms
    - Network Architecture & Design
    - Automation & Programming
    - Certifications
    - Languages
    """

    user_name = _extract_user_name(life_story)
    domain = job_analysis.get("domain", "service_provider")
    # Map domains to which skill categories should lead
    # These match Sergio's actual skills.tex categories
    domain_priority = {
        "service_provider":       ["Routing Protocols", "Network Architecture & Design", "Network Platforms"],
        "cloud_networking":       ["Network Platforms", "Automation & Programming", "Routing Protocols"],
        "enterprise_networking":  ["Network Architecture & Design", "Routing Protocols", "Network Platforms"],
        "network_automation":     ["Automation & Programming", "Routing Protocols", "Network Platforms"],
        "network_security":       ["Network Architecture & Design", "Routing Protocols", "Automation & Programming"],
        "data_center_networking": ["Network Platforms", "Network Architecture & Design", "Routing Protocols"],
        "government_network":     ["Network Architecture & Design", "Routing Protocols", "Network Platforms"],
        "network_education":      ["Routing Protocols", "Network Platforms", "Automation & Programming"],
    }
    priority_cats = domain_priority.get(domain, ["Routing Protocols", "Network Architecture & Design"])

    system = f"""You are an expert CV writer helping {user_name} apply for senior data roles.
You produce LaTeX using the curve document class rubric format.
Output ONLY valid LaTeX — no markdown, no explanations, no code fences."""

    prompt = f"""Customize the skills section of {user_name}'s CV for this specific job.

TARGET JOB:
- Domain: {domain}
- Key Technologies: {', '.join(job_analysis.get('key_technologies', []))}
- Focus Areas: {', '.join(job_analysis.get('focus_areas', []))}

PRIORITY SKILL CATEGORIES FOR THIS DOMAIN (list these first):
{chr(10).join(f'- {c}' for c in priority_cats)}

BASE TEMPLATE (current skills.tex — all categories and their content):
{base_template}

RULES:
1. Keep ALL categories from the base template. Do not remove any.
2. Reorder categories so the priority ones listed above appear first.
3. Within each category, move the most job-relevant skills to the front of the list.
4. Do NOT add skills not present in the base template.
5. Do NOT rename categories.
6. Keep all content within each category — only reorder within and across categories.
7. Use \\entry*[Category Name]% format for each category.
8. Use \\textbullet{{}} at the start of each entry's skill list.
9. Use \\& for ampersand, escape % as \\%.
10. Use the EXACT format: \\begin{{rubric}}{{Skills}} ... \\end{{rubric}}

Output the complete skills.tex content:"""

    result = generate_latex(prompt, system=system, model=model, max_tokens=2000)

    if "\\begin{rubric}" not in result or "\\end{rubric}" not in result:
        logger.warning(
            "Generated skills.tex is missing rubric structure — falling back to base template. "
            "Check that Ollama is running the 9b model and try again."
        )
        return base_template

    return result


def generate_projects_tex(
    life_story: str,
    job_analysis: Dict,
    base_template: str,
    model: str = "qwen3.5:9b",
) -> str:
    """Generate a customized projects.tex for a specific job."""

    user_name = _extract_user_name(life_story)

    system = f"""You are an expert CV writer helping {user_name} apply for senior network engineering roles.
You produce LaTeX using the curve document class rubric format.
Output ONLY valid LaTeX — no markdown, no explanations, no code fences.
{CV_WRITING_RULES}"""

    prompt = f"""Customize the projects section of {user_name}'s CV for this specific job.

TARGET JOB:
- Domain: {job_analysis.get('domain', 'service_provider')}
- Key Technologies: {', '.join(job_analysis.get('key_technologies', []))}
- Focus Areas: {', '.join(job_analysis.get('focus_areas', []))}

{user_name.upper()}'S FULL BACKGROUND (life-story.md):
{life_story[:4000]}

BASE TEMPLATE (current projects.tex):
{base_template}

RULES:
1. Select the 3-4 most relevant projects for the target job domain.
2. Order them by relevance — most relevant first.
3. Adjust descriptions to surface job-relevant outcomes without inventing new ones.
4. Keep \\href{{}}{{\\faGithub}} links intact if present.
5. Use \\entry*[year]% format.
6. Use \\begin{{rubric}}{{Projects}} ... \\end{{rubric}}.
7. Do NOT invent projects, metrics, or technologies not in the background above.

Output the complete projects.tex content:"""

    result = generate_latex(prompt, system=system, model=model, max_tokens=2000)

    if "\\begin{rubric}" not in result or "\\end{rubric}" not in result:
        logger.warning(
            "Generated projects.tex is missing rubric structure — falling back to base template. "
            "Check that Ollama is running the 9b model and try again."
        )
        return base_template

    return result


def validate_latex(content: str) -> bool:
    """Basic validation of LaTeX content."""
    opens = content.count("\\begin{rubric}")
    closes = content.count("\\end{rubric}")
    if opens != closes or opens == 0:
        return False
    diff = abs(content.count("{") - content.count("}"))
    if diff > 2:
        return False
    return True


def _extract_section(text: str, header: str) -> str:
    pattern = rf"^##\s+{re.escape(header)}\s*$"
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(pattern, ln.strip(), flags=re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return ""
    out: list[str] = []
    for ln in lines[start:]:
        if ln.strip().startswith("## "):
            break
        out.append(ln)
    return "\n".join(out).strip()


def _md_bullets(block: str) -> list[str]:
    bullets: list[str] = []
    for ln in (block or "").splitlines():
        s = ln.strip()
        if s.startswith("- "):
            bullets.append(s[2:].strip())
    return bullets


def _entry(date: str, body_lines: list[str]) -> str:
    date = (date or "").replace("–", "--")
    out = [rf"\entry*[{_latex_escape(date)}]%"]
    for idx, ln in enumerate(body_lines):
        if idx == 0:
            out.append(f"    {ln}")
        else:
            out.append(f"    \\par {ln}")
    return "\n".join(out)


def _parse_work_experience(text: str) -> list[dict]:
    block = _extract_section(text, "Work Experience")
    if not block:
        return []
    lines = block.splitlines()
    entries: list[dict] = []
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("### "):
            title = ln[4:].strip()
            i += 1
            date = ""
            if i < len(lines) and lines[i].strip().startswith("**") and lines[i].strip().endswith("**"):
                date = lines[i].strip().strip("*").strip()
                i += 1
            body_lines: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("### "):
                body_lines.append(lines[i])
                i += 1
            body = "\n".join(body_lines).strip()
            tech = ""
            m = re.search(r"\*\*Technologies:\*\*\s*(.+)", body)
            if m:
                tech = m.group(1).strip()
            bullets = _md_bullets(body)
            bullets = [b for b in bullets if not b.lower().startswith("technologies:")]
            paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
            context = ""
            if paras:
                context = re.sub(r"\*\*Technologies:\*\*.*", "", paras[0]).strip()
            entries.append({"title": title, "date": date, "context": context, "bullets": bullets, "tech": tech})
        else:
            i += 1
    return entries


def _parse_skills(text: str) -> list[tuple[str, str]]:
    block = _extract_section(text, "Skills")
    if not block:
        return []
    lines = block.splitlines()
    out: list[tuple[str, str]] = []
    current = ""
    items: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("### "):
            if current and items:
                out.append((current, ", ".join(items)))
            current = s[4:].strip()
            items = []
        elif s.startswith("- "):
            items.append(s[2:].split("—", 1)[0].strip())
    if current and items:
        out.append((current, ", ".join(items)))
    return out


def _parse_projects(text: str) -> list[dict]:
    block = _extract_section(text, "Projects")
    if not block:
        return []
    lines = block.splitlines()
    entries: list[dict] = []
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("### "):
            name = ln[4:].strip()
            i += 1
            body_lines: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("### "):
                body_lines.append(lines[i])
                i += 1
            body = "\n".join(body_lines).strip()
            bullets = _md_bullets(body)
            code = tech = what = achievements = ""
            for b in bullets:
                bl = b.lower()
                if bl.startswith("code:"):
                    code = b.split(":", 1)[1].strip()
                elif bl.startswith("technologies:"):
                    tech = b.split(":", 1)[1].strip()
                elif bl.startswith("what it does:"):
                    what = b.split(":", 1)[1].strip()
                elif bl.startswith("key achievements:"):
                    achievements = b.split(":", 1)[1].strip()
            entries.append({"name": name, "what": what, "achievements": achievements, "tech": tech, "code": code})
        else:
            i += 1
    return entries


def render_employment_from_life_story(text: str) -> str:
    entries = _parse_work_experience(text)
    lines = [r"\begin{rubric}{Experience}", ""]
    if not entries:
        lines += [r"\end{rubric}", ""]
        return "\n".join(lines).strip()
    for e in entries:
        title = e["title"]
        role = title
        org = ""
        if "—" in title:
            role, org = [p.strip() for p in title.split("—", 1)]
        head = rf"\textbf{{{_latex_escape(role)},}} {_latex_escape(org)}.".strip()
        body: list[str] = [head]
        if e.get("context"):
            body.append(_latex_escape(e["context"]))
        for b in e.get("bullets", [])[:6]:
            body.append(rf"- {_latex_escape(b)}")
        if e.get("tech"):
            body.append(rf"Technologies: {_latex_escape(e['tech'])}.")
        lines.append(_entry(e.get("date", ""), body))
        lines.append("")
    lines.append(r"\end{rubric}")
    return "\n".join(lines).strip() + "\n"


def render_skills_from_life_story(text: str) -> str:
    cats = _parse_skills(text)
    lines = [r"\begin{rubric}{Skills}", ""]
    for cat, items in cats:
        lines.append(rf"\entry*[{_latex_escape(cat)}]%")
        lines.append(f"    {_latex_escape(items)}.")
        lines.append("")
    lines.append(r"\end{rubric}")
    return "\n".join(lines).strip() + "\n"


def render_projects_from_life_story(text: str) -> str:
    projs = _parse_projects(text)
    lines = [r"\begin{rubric}{Projects}", ""]
    for p in projs:
        body: list[str] = [
            rf"\textbf{{{_latex_escape(p['name'])}}} — "
            rf"{_latex_escape(p.get('what') or p.get('achievements') or '')}"
        ]
        if p.get("tech"):
            body.append(rf"Technologies: {_latex_escape(p['tech'])}.")
        if p.get("code"):
            body.append(rf"\href{{{p['code']}}}{{\faGithub}}")
        lines.append(_entry("2025", body))
        lines.append("")
    lines.append(r"\end{rubric}")
    return "\n".join(lines).strip() + "\n"


def ensure_base_cv_content(cv_dir: Path, *, model: str = "qwen2.5:3b") -> None:
    """If cv_dir contains template placeholders, regenerate from life-story.md."""
    life_story_path = resolve_life_story_path(cv_dir)
    if not life_story_path.exists():
        return
    life_story = life_story_path.read_text(encoding="utf-8", errors="replace")

    renderers = {
        "employment.tex": render_employment_from_life_story,
        "skills.tex":     render_skills_from_life_story,
        "projects.tex":   render_projects_from_life_story,
    }

    for filename, renderer in renderers.items():
        path = cv_dir / filename
        if not path.exists():
            continue
        current = path.read_text(encoding="utf-8", errors="replace")
        if not _looks_like_placeholder(current):
            continue
        try:
            path.write_text(renderer(life_story), encoding="utf-8")
            logger.info("Regenerated base %s from life-story.md", filename)
        except Exception as e:
            logger.warning("Failed to regenerate base %s: %s", filename, e)


def ensure_cv_scaffold(cv_dir: Path) -> None:
    """Ensure cv_dir contains the minimum required template files."""
    templates_dir = _PROJECT_ROOT / "cv_templates"
    if not templates_dir.exists():
        return

    cv_dir.mkdir(parents=True, exist_ok=True)

    mapping = {
        "cv-llt-template.tex":        "cv-llt.tex",
        "employment-template.tex":    "employment.tex",
        "skills-template.tex":        "skills.tex",
        "projects-template.tex":      "projects.tex",
        "education-template.tex":     "education.tex",
        "publications-template.tex":  "publications.tex",
        "own-bib.bib":                "own-bib.bib",
        "settings.sty":               "settings.sty",
    }

    for src_name, dest_name in mapping.items():
        src = templates_dir / src_name
        dest = cv_dir / dest_name
        if src.exists() and not dest.exists():
            shutil.copy2(str(src), str(dest))

    for optional in ["publications.tex", "own-bib.bib"]:
        dest = cv_dir / optional
        if not dest.exists():
            try:
                dest.write_text("", encoding="utf-8")
            except Exception:
                pass

    project_life = _PROJECT_ROOT / "life-story.md"
    if not project_life.exists():
        src = templates_dir / "life_story_template.md"
        dest = cv_dir / "life-story.md"
        if src.exists() and not dest.exists():
            shutil.copy2(str(src), str(dest))


def resolve_cv_dir(profile: Optional[dict] = None) -> Path:
    raw = (profile or {}).get("pipeline", {}).get("cv_dir", "")
    if raw:
        return Path(os.path.expanduser(raw))
    return _DEFAULT_CV_DIR


def resolve_life_story_path(cv_dir: Path) -> Path:
    project = _PROJECT_ROOT / "life-story.md"
    return project if project.exists() else cv_dir / "life-story.md"


# Back-compat module-level accessors
CV_DIR = _DEFAULT_CV_DIR
LIFE_STORY_PATH = resolve_life_story_path(CV_DIR)


def create_application_dir(slug: str, cv_dir: Path) -> Path:
    """Create an application directory with copies and symlinks."""
    applications_dir = cv_dir / "applications"
    dest = applications_dir / slug
    if dest.exists():
        logger.info("Application dir already exists: %s", dest)
        return dest

    dest.mkdir(parents=True, exist_ok=True)

    for f in ["cv-llt.tex"]:
        src = cv_dir / f
        if src.exists():
            shutil.copy2(str(src), str(dest / f))

    for f in SYMLINK_FILES:
        link = dest / f
        target = cv_dir / f
        if not link.exists() and target.exists():
            try:
                link.symlink_to(target)
            except OSError as e:
                logger.warning("Failed to create symlink %s: %s (copying instead)", f, e)
                try:
                    shutil.copy2(str(target), str(link))
                except Exception as copy_err:
                    logger.warning("Failed to copy %s: %s", f, copy_err)

    return dest


def compile_latex(directory: Path) -> Optional[str]:
    """Compile LaTeX to PDF in the given directory. Returns PDF path or None."""
    tex_file = directory / "cv-llt.tex"
    if not tex_file.exists():
        logger.error("No cv-llt.tex found in %s", directory)
        return None

    def _cleanup():
        for ext in [".aux", ".bbl", ".bcf", ".blg", ".fdb_latexmk", ".fls",
                    ".log", ".out", ".run.xml", ".synctex.gz", ".toc"]:
            aux = directory / ("cv-llt" + ext)
            if aux.exists():
                try:
                    aux.unlink()
                except Exception:
                    pass

    pdf_path = directory / "cv-llt.pdf"
    ensure_miktex_auto_install()

    try:
        result = subprocess.run(
            ["latexmk", "-pdf", "-interaction=nonstopmode", "cv-llt.tex"],
            cwd=str(directory),
            capture_output=True, text=True, timeout=180,
        )
        if _is_valid_pdf(pdf_path):
            _cleanup()
            return str(pdf_path)
        stderr = (result.stderr or "")[-2000:]
        if "script engine 'perl'" not in stderr.lower():
            logger.error("PDF not generated. LaTeX output:\n%s", stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("latexmk unavailable/failed (%s). Falling back to pdflatex.", e)

    miktex_bin = Path(r"C:\Users\rebec\AppData\Local\Programs\MiKTeX\miktex\bin\x64")
    pdflatex = miktex_bin / "pdflatex.exe"
    pdflatex_cmd = str(pdflatex) if pdflatex.exists() else "pdflatex"
    try:
        last = None
        for _ in range(2):
            last = subprocess.run(
                [pdflatex_cmd, "-interaction=nonstopmode", "cv-llt.tex"],
                cwd=str(directory),
                capture_output=True, text=True, timeout=900,
            )
        if _is_valid_pdf(pdf_path):
            _cleanup()
            return str(pdf_path)
        if pdf_path.exists() and not _is_valid_pdf(pdf_path):
            try:
                pdf_path.unlink()
            except Exception:
                pass
        if last is not None:
            try:
                (directory / "cv-compile.stdout.txt").write_text(last.stdout or "", encoding="utf-8", errors="replace")
                (directory / "cv-compile.stderr.txt").write_text(last.stderr or "", encoding="utf-8", errors="replace")
            except Exception:
                pass
            tail = ((last.stderr or last.stdout or "")[-2500:]).strip()
            if tail:
                logger.error("pdflatex output (tail):\n%s", tail)
        logger.error("PDF not generated via pdflatex either.")
        return None
    except Exception as e:
        logger.error("pdflatex failed: %s", e)
        if pdf_path.exists() and not _is_valid_pdf(pdf_path):
            try:
                pdf_path.unlink()
            except Exception:
                pass
        return None


def customize_cv_for_job(
    job_url: str,
    title: str,
    company: str,
    location: str,
    description: str,
    model: str = "qwen3.5:9b",
    profile: Optional[dict] = None,
) -> Optional[Dict]:
    """Full CV customization pipeline for a job.

    Returns dict with: slug, cv_pdf_path, app_dir, or None on failure.
    """
    if not check_ollama_available():
        logger.error(
            "Ollama is not running. CV customization requires a local LLM.\n"
            "Install and start it: bash setup_ollama.sh"
        )
        return None

    cv_dir = resolve_cv_dir(profile)
    ensure_cv_scaffold(cv_dir)
    ensure_base_cv_content(cv_dir, model=model)
    life_story_path = resolve_life_story_path(cv_dir)

    life_story = _read_file(life_story_path)
    if not life_story:
        logger.error(
            "life-story.md not found. Expected at: %s\n"
            "Copy cv_templates/life_story_template.md to that path and fill it in.",
            life_story_path,
        )
        return None

    base_employment = _read_file(cv_dir / "employment.tex")
    base_skills     = _read_file(cv_dir / "skills.tex")
    base_projects   = _read_file(cv_dir / "projects.tex")

    slug = _slugify(f"{company}-{title}") or _slugify(company or "unknown")

    logger.info("Customizing CV for: %s at %s (slug: %s)", title, company, slug)

    # Step 1: Analyze job
    logger.info("Step 1: Analyzing job description...")
    job_analysis = analyze_job(description, title, company, model=model)
    logger.info("Job domain: %s | Seniority: %s", job_analysis.get("domain"), job_analysis.get("seniority"))

    # Step 2: Generate customized LaTeX with retry
    used_fallback = False
    for attempt in range(3):
        logger.info("Step 2: Generating LaTeX (attempt %d/3)...", attempt + 1)

        employment_tex = generate_employment_tex(life_story, job_analysis, base_employment, model=model)
        skills_tex     = generate_skills_tex(life_story, job_analysis, base_skills, model=model)
        projects_tex   = generate_projects_tex(life_story, job_analysis, base_projects, model=model)

        valid = all([
            validate_latex(employment_tex),
            validate_latex(skills_tex),
            validate_latex(projects_tex),
        ])

        if valid:
            break

        logger.warning("LaTeX validation failed on attempt %d/3.", attempt + 1)

        if attempt == 2:
            logger.warning(
                "All 3 generation attempts failed validation. "
                "Falling back to base templates — this PDF will NOT be tailored to the job. "
                "Try running again or check that qwen3.5:9b is loaded in Ollama."
            )
            employment_tex = base_employment
            skills_tex     = base_skills
            projects_tex   = base_projects
            used_fallback  = True

    # Step 3: Create application directory
    logger.info("Step 3: Creating application directory...")
    app_dir = create_application_dir(slug, cv_dir)

    (app_dir / "employment.tex").write_text(employment_tex, encoding="utf-8")
    (app_dir / "skills.tex").write_text(skills_tex, encoding="utf-8")
    (app_dir / "projects.tex").write_text(projects_tex, encoding="utf-8")
    personalize_cv_header(app_dir)

    # Save job description for reference
    jd_content = (
        f"# {title} at {company}\n\n"
        f"**Location:** {location}\n"
        f"**URL:** {job_url}\n\n"
        f"---\n\n{description}"
    )
    (app_dir / "job-description.md").write_text(jd_content, encoding="utf-8")

    # Step 4: Compile
    logger.info("Step 4: Compiling LaTeX to PDF...")
    pdf_path = compile_latex(app_dir)

    if pdf_path:
        if used_fallback:
            logger.warning("PDF generated but used base (untailored) templates: %s", pdf_path)
        else:
            logger.info("Tailored CV generated: %s", pdf_path)
        return {
            "slug":         slug,
            "cv_pdf_path":  pdf_path,
            "app_dir":      str(app_dir),
            "tailored":     not used_fallback,
            "job_analysis": job_analysis,
        }

    # Final fallback: retry compilation with base templates
    logger.error("PDF compilation failed. Retrying with base templates...")
    (app_dir / "employment.tex").write_text(base_employment, encoding="utf-8")
    (app_dir / "skills.tex").write_text(base_skills, encoding="utf-8")
    (app_dir / "projects.tex").write_text(base_projects, encoding="utf-8")
    pdf_path = compile_latex(app_dir)

    if pdf_path:
        logger.warning(
            "PDF generated using base (untailored) templates as last resort: %s\n"
            "The CV was not customized for this job. Check LaTeX logs in %s.",
            pdf_path, app_dir
        )
        return {
            "slug":         slug,
            "cv_pdf_path":  pdf_path,
            "app_dir":      str(app_dir),
            "tailored":     False,
            "job_analysis": job_analysis,
        }

    logger.error("CV generation failed entirely for %s at %s. See logs in %s.", title, company, app_dir)
    return None