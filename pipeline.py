"""Pipeline orchestrator — background daemon for automated job application.

Runs the full loop: scrape → match → customize CV → cover letter → form answers → email.
Can run as a one-shot or as a daemon on a configurable interval.

Usage:
    python main.py pipeline                         # one shot
    python main.py pipeline --dry-run               # preview without generating files
    python main.py pipeline --max 5 --threshold 0.6 # cap and threshold override
    python main.py daemon                           # run every 48 hours
    python main.py daemon --interval 24             # run every 24 hours
"""

import json
import logging
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from models import Job, JobBoard, SearchQuery
from scrapers import SCRAPERS
from matcher import JobMatcher
from storage import (
    save_jobs, get_db, get_top_jobs,
    create_application, update_application, get_application_by_job,
    start_pipeline_run, finish_pipeline_run,
    get_new_jobs_since, get_last_email_sent,
)
from cv_customizer import customize_cv_for_job, LIFE_STORY_PATH
from cover_letter import create_cover_letter
from form_answers import generate_form_answers
from notifier import send_digest_email, should_send_digest

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "profile.yaml"

# Boards where location is handled internally (API or fixed region).
# These are deduplicated by (board, keyword) rather than (board, keyword, location)
# so we don't scrape them once per location query.
LOCATION_AGNOSTIC_BOARDS = {
    "remotive",
    "arbeitnow",
    "himalayas",
    "greenhouse",
    "lever",
    "linkedin_posts",
    "internet",
    "usajobs",       # location passed directly to API, not per search query
}

# Graceful shutdown flag for daemon mode
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received — finishing current cycle before stopping.")
    _shutdown = True


def load_profile() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"profile.yaml not found at {CONFIG_PATH}. "
            "Run: python main.py init-profile"
        )
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Step 1: Scrape ─────────────────────────────────────────────────────────────

def _scrape_all(profile: dict, max_per_query: int = 50) -> List[Job]:
    """Scrape all configured boards. Returns a deduplicated job list."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    search      = profile.get("search", {})
    board_names = search.get("boards", ["indeed", "linkedin", "glassdoor"])
    boards      = []

    for b in board_names:
        try:
            boards.append(JobBoard(b))
        except ValueError:
            logger.warning("Unknown board '%s' in profile.yaml — skipping.", b)

    if not boards:
        logger.error("No valid boards configured in profile.yaml.")
        return []

    queries = []
    for kw in search.get("queries", ["director of data analytics"]):
        for loc in search.get("locations", [""]):
            queries.append(SearchQuery(
                keywords     = kw,
                location     = loc,
                remote       = search.get("remote", False),
                max_age_days = search.get("max_age_days", 14),
                boards       = boards,
            ))

    futures      = []
    seen_combos  = set()
    all_jobs: List[Job] = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        for query in queries:
            for board in query.boards:
                board_name = board.value
                if board_name in LOCATION_AGNOSTIC_BOARDS:
                    combo = (board_name, query.keywords)
                    if combo in seen_combos:
                        continue
                    seen_combos.add(combo)

                scraper_cls = SCRAPERS.get(board_name)
                if not scraper_cls:
                    logger.warning(
                        "No scraper registered for board '%s'. "
                        "Check scrapers/__init__.py.", board_name
                    )
                    continue

                fut = pool.submit(_scrape_one, scraper_cls, query, max_per_query)
                futures.append((board_name, fut))

        for board_name, fut in futures:
            try:
                results = fut.result()
                all_jobs.extend(results)
                logger.info("[%s] scraped %d jobs", board_name, len(results))
            except Exception as e:
                logger.error("[%s] scraper error: %s", board_name, e)

    # Deduplicate by URL and by title+company fingerprint
    seen_urls: set[str] = set()
    seen_fp:   set[str] = set()
    unique: List[Job]   = []

    for j in all_jobs:
        fp = f"{j.title.lower().strip()}|{j.company.lower().strip()}"
        if j.url not in seen_urls and fp not in seen_fp:
            seen_urls.add(j.url)
            seen_fp.add(fp)
            unique.append(j)

    logger.info(
        "Scraping complete: %d total → %d unique after deduplication",
        len(all_jobs), len(unique),
    )
    return unique


def _scrape_one(scraper_cls, query: SearchQuery, max_results: int) -> List[Job]:
    scraper = scraper_cls()
    return scraper.scrape(query, max_results=max_results)


# ── Main pipeline ──────────────────────────────────────────────────────────────

def run_pipeline(
    profile: Optional[dict] = None,
    dry_run: bool = False,
    max_applications: int = 10,
    threshold: float = 0.5,
    model: str = "qwen3.5:9b",
) -> Dict:
    """Run one full pipeline cycle.

    Returns dict with stats:
        jobs_scraped, jobs_matched, applications_created,
        applications_failed, emails_sent.
    """
    if profile is None:
        profile = load_profile()

    pipeline_config  = profile.get("pipeline", {})
    threshold        = pipeline_config.get("auto_apply_threshold", threshold)
    max_applications = pipeline_config.get("max_applications_per_run", max_applications)
    model            = pipeline_config.get("ollama_model", model)
    interval_days    = pipeline_config.get("email_digest_interval_days", 2)

    # Recipient must be set in profile.yaml or .env — no hardcoded fallback
    recipient = (
        pipeline_config.get("email_recipient")
        or os.environ.get("NOTIFY_EMAIL", "")
    )
    if not recipient:
        logger.warning(
            "No email recipient configured. "
            "Set 'email_recipient' in profile.yaml or NOTIFY_EMAIL in .env. "
            "Digest email will be skipped."
        )

    logger.info(
        "Pipeline starting — model: %s | threshold: %.2f | max: %d | dry_run: %s",
        model, threshold, max_applications, dry_run,
    )

    run_id = start_pipeline_run()
    stats = {
        "jobs_scraped":          0,
        "jobs_matched":          0,
        "applications_created":  0,
        "applications_failed":   0,
        "emails_sent":           0,
    }
    log_lines: List[str] = []

    try:
        # ── Step 1: Load life story (required for all generation steps) ────────
        logger.info("=== Step 1: Loading life story ===")
        if not LIFE_STORY_PATH.exists():
            logger.error(
                "life-story.md not found at %s. "
                "This file is required for CV and cover letter generation. "
                "Copy cv_templates/life_story_template.md and fill it in.",
                LIFE_STORY_PATH,
            )
            finish_pipeline_run(run_id, status="failed", log="life-story.md missing", **stats)
            return stats

        life_story = LIFE_STORY_PATH.read_text(encoding="utf-8").strip()
        if not life_story:
            logger.error(
                "life-story.md exists but is empty at %s. "
                "Fill it in before running the pipeline.",
                LIFE_STORY_PATH,
            )
            finish_pipeline_run(run_id, status="failed", log="life-story.md empty", **stats)
            return stats

        logger.info("life-story.md loaded (%d characters).", len(life_story))

        # ── Step 2: Scrape ─────────────────────────────────────────────────────
        logger.info("=== Step 2: Scraping ===")
        if not dry_run:
            jobs   = _scrape_all(profile)
            matcher = JobMatcher(profile)
            ranked  = matcher.rank(jobs)
            n_saved = save_jobs(ranked)
            stats["jobs_scraped"] = len(ranked)
            msg = f"Scraped {len(ranked)} jobs, {n_saved} new saved to DB."
            log_lines.append(msg)
            logger.info(msg)
        else:
            logger.info("[DRY RUN] Skipping scrape — using existing jobs from DB.")

        # ── Step 3: Select candidates ──────────────────────────────────────────
        logger.info("=== Step 3: Selecting candidates (score >= %.2f) ===", threshold)
        top_jobs   = get_top_jobs(limit=max_applications * 3, min_score=threshold)
        candidates = []

        for job in top_jobs:
            existing = get_application_by_job(job["url"])
            if not existing and job.get("description"):
                candidates.append(job)
            if len(candidates) >= max_applications:
                break

        stats["jobs_matched"] = len(candidates)
        logger.info(
            "Selected %d candidate jobs (from %d above threshold, capped at %d).",
            len(candidates), len(top_jobs), max_applications,
        )

        if not candidates:
            logger.info(
                "No new candidates to process. "
                "Try lowering --threshold or running a fresh scrape."
            )

        # ── Step 4: Generate applications ──────────────────────────────────────
        logger.info("=== Step 4: Generating applications ===")

        for i, job in enumerate(candidates):
            if _shutdown:
                logger.info("Shutdown requested — stopping after %d applications.", i)
                break

            logger.info(
                "[%d/%d] %s at %s (score: %.2f)",
                i + 1, len(candidates),
                job["title"], job["company"], job["match_score"],
            )

            if dry_run:
                logger.info(
                    "[DRY RUN] Would generate: CV + cover letter + form answers for %s at %s",
                    job["title"], job["company"],
                )
                continue

            try:
                # 4a: Customize CV — returns job_analysis so we don't call it twice
                cv_result = customize_cv_for_job(
                    job_url     = job["url"],
                    title       = job["title"],
                    company     = job["company"],
                    location    = job.get("location", ""),
                    description = job.get("description", ""),
                    model       = model,
                    profile     = profile,
                )

                if not cv_result:
                    msg = f"FAILED (CV): {job['title']} at {job['company']}"
                    logger.error(msg)
                    log_lines.append(msg)
                    stats["applications_failed"] += 1
                    continue

                app_id = create_application(job["url"], cv_result["slug"])
                update_application(
                    app_id,
                    status       = "cv_generated",
                    cv_pdf_path  = cv_result["cv_pdf_path"],
                )

                if not cv_result.get("tailored"):
                    logger.warning(
                        "CV for %s at %s used base templates (not tailored). "
                        "Check Ollama logs.",
                        job["title"], job["company"],
                    )

                # 4b: Reuse job_analysis from CV step if available,
                #     otherwise analyze now (avoids a second LLM call)
                job_analysis = cv_result.get("job_analysis")
                if not job_analysis:
                    from cv_customizer import analyze_job
                    job_analysis = analyze_job(
                        job.get("description", ""),
                        job["title"],
                        job["company"],
                        model=model,
                    )

                # 4c: Cover letter
                cl_path = create_cover_letter(
                    app_dir     = cv_result["app_dir"],
                    title       = job["title"],
                    company     = job["company"],
                    location    = job.get("location", ""),
                    description = job.get("description", ""),
                    life_story  = life_story,
                    job_analysis = job_analysis,
                    model       = model,
                )

                if cl_path:
                    update_application(
                        app_id,
                        status                  = "letter_generated",
                        cover_letter_pdf_path   = cl_path,
                    )
                else:
                    logger.warning(
                        "Cover letter generation failed for %s at %s.",
                        job["title"], job["company"],
                    )

                # 4d: Form answers
                answers = generate_form_answers(
                    life_story   = life_story,
                    title        = job["title"],
                    company      = job["company"],
                    description  = job.get("description", ""),
                    job_analysis = job_analysis,
                    model        = model,
                )

                if answers:
                    update_application(
                        app_id,
                        status              = "ready",
                        form_answers_json   = json.dumps(answers),
                    )
                else:
                    logger.warning(
                        "Form answer generation failed for %s at %s.",
                        job["title"], job["company"],
                    )
                    update_application(app_id, status="ready")

                stats["applications_created"] += 1
                tailored_flag = "" if cv_result.get("tailored", True) else " [base template]"
                msg = f"OK{tailored_flag}: {job['title']} at {job['company']}"
                log_lines.append(msg)
                logger.info("Application ready: %s", cv_result["slug"])

            except Exception as e:
                msg = f"ERROR: {job['title']} at {job['company']}: {e}"
                logger.error(msg, exc_info=True)
                log_lines.append(msg)
                stats["applications_failed"] += 1

        # ── Step 5: Email digest ───────────────────────────────────────────────
        logger.info("=== Step 5: Email digest ===")

        if dry_run:
            logger.info("[DRY RUN] Skipping email.")
        elif not recipient:
            logger.info("No recipient configured — skipping email.")
        elif should_send_digest(interval_days):
            last_email = get_last_email_sent()
            since      = last_email["sent_at"] if last_email else "2000-01-01T00:00:00"
            new_jobs   = get_new_jobs_since(since, min_score=threshold)

            if new_jobs:
                success = send_digest_email(new_jobs, recipient)
                if success:
                    stats["emails_sent"] = 1
                    log_lines.append(f"Digest sent: {len(new_jobs)} jobs to {recipient}")
                else:
                    log_lines.append("Digest send failed — check Gmail credentials in .env")
            else:
                logger.info("No new jobs since last digest — skipping email.")
        else:
            logger.info("Digest not due yet (interval: %d days).", interval_days)

        # ── Done ───────────────────────────────────────────────────────────────
        finish_pipeline_run(
            run_id,
            status = "completed",
            log    = "\n".join(log_lines),
            **stats,
        )

        logger.info(
            "Pipeline complete — scraped: %d | matched: %d | "
            "created: %d | failed: %d | emails: %d",
            stats["jobs_scraped"],
            stats["jobs_matched"],
            stats["applications_created"],
            stats["applications_failed"],
            stats["emails_sent"],
        )

    except Exception as e:
        logger.error("Pipeline failed unexpectedly: %s", e, exc_info=True)
        finish_pipeline_run(run_id, status="failed", log=str(e), **stats)

    return stats


# ── Daemon ─────────────────────────────────────────────────────────────────────

def run_daemon(interval_hours: float = 48.0):
    """Run the pipeline in a loop on a fixed interval.

    Default interval is 48 hours (2 days). Send SIGTERM or press Ctrl+C
    to stop gracefully after the current cycle completes.
    """
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    interval_seconds = interval_hours * 3600

    logger.info(
        "Daemon started — interval: %.1f hours. Press Ctrl+C to stop gracefully.",
        interval_hours,
    )

    while not _shutdown:
        cycle_start = datetime.now()
        logger.info("=== Daemon cycle starting at %s ===", cycle_start.isoformat())

        try:
            stats = run_pipeline()
            logger.info("Cycle complete: %s", stats)
        except Exception as e:
            logger.error("Daemon cycle failed: %s", e, exc_info=True)

        if _shutdown:
            break

        logger.info(
            "Next cycle in %.1f hours. Sleeping... (Ctrl+C to stop)",
            interval_hours,
        )

        # Sleep in 1-second increments so Ctrl+C is responsive
        elapsed = 0
        while elapsed < interval_seconds and not _shutdown:
            time.sleep(1)
            elapsed += 1

    logger.info("Daemon stopped cleanly.")