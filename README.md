# job_finder — Automated Job Search Pipeline

Scrapes job listings from multiple sources, scores them against your profile using semantic embeddings, and for top matches automatically generates a tailored CV, cover letter, and form answers — all driven by a local LLM (Qwen via Ollama).

---

## Features

- **Multi-source scraping** — pulls from Indeed, Glassdoor, Google Jobs, LinkedIn, Greenhouse, and Lever simultaneously
- **Semantic matching** — sentence-transformer embeddings rank jobs against your full life story and profile
- **Automated CV customization** — LLM rewrites `employment.tex`, `skills.tex`, and `projects.tex` for each job and compiles to PDF
- **Automated cover letter generation** — LLM writes a tailored cover letter, compiled to PDF
- **Form answer generation** — LLM pre-answers common application questions (motivation, salary, visa, etc.)
- **Form-fill guide** — maps pre-generated answers to specific form field names for browser-based manual entry
- **Digest email notifier** — sends an HTML email every 2–3 days with new high-match jobs
- **Background daemon** — runs the full pipeline on a configurable interval (default: every 48 hours)
- **Web dashboard** — Flask UI with filtering, sorting, and apply/hide actions
- **CLI tools** — scrape, match, export, customize, run pipeline, and view top jobs from the terminal
- **SQLite storage** — deduplicates and persists all scraped jobs and application state

---

## Supported Job Sources

| Source | Type |
|---|---|
| **Indeed** | Web scraper |
| **Glassdoor** | Web scraper |
| **Google Jobs** | RapidAPI (JSearch) |
| **LinkedIn** | Guest scraper |
| **Greenhouse** | ATS scraper |
| **Lever** | ATS scraper |

Google Jobs requires a RapidAPI key. All other sources work without one.

---

## Quick Start

### 1. Clone & Install

```bash
git clone <your-repo-url> && cd job_finder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Install Ollama

```bash
bash setup_ollama.sh
```

Or manually:

```bash
brew install ollama && ollama pull qwen2.5:3b
```

Use `qwen2.5:3b` on CPU or `qwen3.5:9b` on GPU / Apple Silicon. The 9b model follows complex instructions more reliably.

### 3. Configure Your Profile

Everything lives inside the project directory:

```
job_finder/
├── life-story.md        ← your background (master source of truth)
├── profile.yaml         ← search config (titles, locations, weights)
└── cv/
    ├── cv-llt.tex
    ├── employment.tex
    ├── skills.tex
    ├── projects.tex
    ├── education.tex
    ├── settings.sty
    ├── own-bib.bib
    └── applications/    ← auto-created; one subfolder per application
```

**Step 1 — Fill in your life story**

Edit `life-story.md` in the project root. This file drives both job matching and CV/cover letter generation. A blank template is at `cv_templates/life_story_template.md`.

**Step 2 — Generate profile.yaml**

```bash
python main.py init-profile
```

Or copy and fill in manually:

```bash
cp profile.yaml.example profile.yaml
```

**Step 3 — Set up your LaTeX CV**

```bash
mkdir -p cv/applications
cp cv_templates/cv-llt-template.tex     cv/cv-llt.tex
cp cv_templates/employment-template.tex cv/employment.tex
cp cv_templates/education-template.tex  cv/education.tex
cp cv_templates/skills-template.tex     cv/skills.tex
cp cv_templates/projects-template.tex   cv/projects.tex
cp cv_templates/settings.sty            cv/settings.sty
touch cv/own-bib.bib
```

Fill in the `YOUR_*` placeholders in each file. See `cv_templates/README.md` for LaTeX installation instructions (MiKTeX on Windows, MacTeX on Mac, texlive on Linux).

Job scraping and matching work without any CV setup. LaTeX and Ollama are only required for PDF generation.

### 4. Set API Keys (optional)

```bash
export RAPIDAPI_KEY="your_rapidapi_key"   # for Google Jobs

# For digest emails:
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="your_app_password"
export NOTIFY_EMAIL="you@gmail.com"
```

Add these to a `.env` file in the project root to persist them.

---

## Usage

### Scrape Jobs

```bash
python main.py scrape

# Scrape specific boards only
python main.py scrape --boards indeed linkedin greenhouse

# Also fetch full job descriptions (slower but better matching)
python main.py scrape --fetch-details
```

### Re-Score Jobs

After editing `profile.yaml`, re-score all stored jobs without re-scraping:

```bash
python main.py match

# Only keep jobs above a minimum score
python main.py match --min-score 0.3
```

### View Top Matches

```bash
python main.py top

python main.py top --limit 50 --min-score 0.2
```

### Generate a Tailored Application for One Job

```bash
python main.py customize --url "https://example.com/job/123"
```

Generates a tailored CV (PDF), cover letter (PDF), and form answers. Output is saved to `cv/applications/<company-role-slug>/`.

### Show Pre-Generated Form Answers

```bash
python main.py answers --url "https://example.com/job/123"
```

Prints a fill guide mapping form field names to your pre-generated answers for manual browser entry.

### Run the Full Pipeline

```bash
# Scrape → match → customize → cover letter → form answers → email
python main.py pipeline

# Preview without generating any files
python main.py pipeline --dry-run

# Process up to 5 jobs above a 0.6 threshold
python main.py pipeline --max 5 --threshold 0.6
```

### Run as Background Daemon

```bash
python main.py daemon

# Custom interval in hours
python main.py daemon --interval 24
```

Send `Ctrl+C` for a graceful shutdown after the current cycle completes.

### Export to JSON

```bash
python main.py export -o top_jobs.json
python main.py export --limit 100 --min-score 0.3 -o filtered.json
```

### Launch Web Dashboard

```bash
python main.py ui

python main.py ui --port 8080 --debug
```

Open `http://localhost:5000` in your browser.

---

## How the Pipeline Works

```
scrape (6 boards)
    │
    ▼
semantic match (sentence-transformers + profile embeddings)
    │
    ▼  jobs above threshold
customize CV  ──►  cover letter  ──►  form answers
    │
    ▼
save to DB  ──►  digest email (every 2–3 days)
```

1. **Scrape** — pulls fresh listings from all configured boards in parallel
2. **Match** — encodes each job description and your profile into embeddings, ranks by cosine similarity
3. **Customize CV** — LLM reads `life-story.md` and rewrites `employment.tex`, `skills.tex`, and `projects.tex` to emphasize relevant experience, then compiles to PDF
4. **Cover letter** — LLM writes tailored body paragraphs, a fixed LaTeX wrapper is applied, and the result is compiled to PDF
5. **Form answers** — LLM pre-answers common screening questions (motivation, relocation, salary, visa)
6. **Notify** — sends an HTML digest email with new matches

---

## Customizing LLM Prompts

The prompts that drive CV and cover letter generation live in:

- `cover_letter.py` — cover letter prompt
- `cv_customizer.py` — CV tailoring prompt

Open either file and locate the prompt string (look for a variable containing "write a cover letter" or "rewrite the employment section"). You can append any writing rules or style instructions directly to that string.

The 9b model follows detailed prompt instructions significantly more reliably than the 3b model. If you add detailed style rules and find they are being ignored, switching models in `profile.yaml` under `pipeline.ollama_model` is the most effective fix.

---

## Output Structure

Each application is saved to its own folder:

```
cv/applications/
└── company-role-slug/
    ├── cv-llt.pdf         ← tailored CV
    ├── cover-letter.pdf   ← tailored cover letter
    └── job.json           ← raw job data including source URL
```

---

## Project Structure

```
job_finder/
├── main.py              # CLI entry point
├── app.py               # Flask web dashboard
├── pipeline.py          # Automation orchestrator and daemon loop
├── matcher.py           # Semantic scoring (sentence-transformers)
├── cv_customizer.py     # LLM-driven CV tailoring and LaTeX compilation
├── cover_letter.py      # LLM-driven cover letter generation and LaTeX compilation
├── form_answers.py      # LLM-driven screening question answers
├── form_filler.py       # Field-mapping fill guide for browser entry
├── notifier.py          # Digest email sender (Gmail SMTP)
├── llm.py               # Ollama/Qwen integration
├── models.py            # Data models (Job, JobBoard, SearchQuery)
├── storage.py           # SQLite persistence
├── profile.yaml         # Your search and pipeline configuration
├── life-story.md        # Your background (master source of truth)
├── jobs.db              # SQLite database (created on first run)
└── scrapers/
    ├── base.py
    ├── jsearch.py        # Google Jobs via RapidAPI
    ├── linkedin_guest.py
    ├── indeed.py
    ├── glassdoor.py
    ├── greenhouse.py
    └── lever.py
```

---

## Configuration Reference

### profile.yaml

| Section | Description |
|---|---|
| `skills` | Your technical skills (matched against job descriptions) |
| `titles` | Desired job titles |
| `keywords` | Domain keywords that boost a job's score |
| `search.queries` | Search terms sent to each job board |
| `search.locations` | Locations to search |
| `search.boards` | Which boards to scrape |
| `search.remote` | Include remote positions |
| `search.max_age_days` | Skip jobs older than N days |
| `preferred_locations` | Locations that boost score |
| `seniority_level` | Preferred level: `junior`, `mid`, `senior`, `staff`, `principal` |
| `weights.skills` | Weight for skill keyword overlap |
| `weights.title` | Weight for title match |
| `weights.semantic` | Weight for embedding similarity (recommended: 0.55+) |
| `weights.location` | Weight for location preference |
| `weights.experience` | Weight for life-story overlap |
| `weights.seniority` | Weight for seniority fit |
| `weights.recency` | Weight for posting recency |
| `pipeline.ollama_model` | Ollama model (`qwen2.5:3b` or `qwen3.5:9b`) |
| `pipeline.min_score` | Minimum score to trigger automation |
| `pipeline.max_applications_per_run` | Cap on applications per pipeline run |

---

## License

MIT