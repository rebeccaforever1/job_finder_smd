"""Application form answer pre-generator.

Generates answers to common ATS questions for each job application.
Answers are stored as JSON for copy-paste or auto-fill.
"""

import logging
from typing import Dict, Optional
from llm import generate_structured, check_ollama_available

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Candidate-specific fixed answers
# These are factual and don't need LLM generation.
# ---------------------------------------------------------------------------
VISA_ANSWER = (
    "I am a US citizen and do not require visa sponsorship."
)

START_DATE_ANSWER = (
    "I can start within 30 days, allowing time to transition my current "
    "work commitments."
)

SALARY_ANSWER = (
    "I am open to discussing compensation based on the full package and role scope. "
    "My expectation is aligned with market rates for principal-level network "
    "engineering roles in this location and industry."
)

LEAVING_ANSWER = (
    "I am looking to move into a role focused on network design and architecture "
    "at a service provider or large enterprise, where I can apply my SP routing "
    "expertise and automation experience at scale."
)

# ---------------------------------------------------------------------------
# Questions the LLM will generate personalized answers for
# ---------------------------------------------------------------------------
GENERATED_QUESTIONS = [
    "Why do you want to work at {company}?",
    "What are your greatest strengths?",
    "Describe a challenging technical project you worked on.",
    "Tell us about yourself.",
    "What is your experience with {tech}?",
    "How do you approach building and leading data teams?",
    "Describe a time you used data to influence a major business decision.",
    "What does a great data culture look like to you?",
]

# Questions with fixed answers — LLM does not generate these
FIXED_QUESTIONS = {
    "Why are you leaving your current position?":   LEAVING_ANSWER,
    "Where do you see yourself in 5 years?":        None,  # LLM generates this one
    "What is your expected salary?":                SALARY_ANSWER,
    "What is your earliest start date?":            START_DATE_ANSWER,
    "Do you require visa sponsorship?":             VISA_ANSWER,
}

# Writing rules — consistent with cover letter and CV
ANSWER_WRITING_RULES = """
WRITING RULES — follow these exactly:

- Every answer must consist of direct affirmative statements.
- Do not use not-X-but-Y structures. State the positive directly.
- No em dashes.
- No rhetorical fragments used for emphasis.
- No stacked parallel hype phrases (e.g. "Real X. Real Y. Real Z.").
- No abstract nouns without measurable or observable referents.
- No superlatives like "best," "top," or "ideal" unless defined by a metric.
- No contrast framing. Write only direct, affirmative statements.
- One complete sentence per idea.
- Each sentence must contain an actor, action, or measurable object.
- Prefer verbs tied to operations or outcomes over nouns tied to concepts.
- Do not use: leverage, cadence, touchpoint, anchor, framing, lever, moment,
  signal, belonging, alignment, synergy, spearheaded, championed, revolutionized,
  transformed (unless tied to a specific metric).
- Combine related observations when they describe the same entity or metric.
- 2-4 sentences per answer. Professional but direct.
"""


def generate_form_answers(
    life_story: str,
    title: str,
    company: str,
    description: str,
    job_analysis: Dict,
    model: str = "qwen3.5:9b",
) -> Dict[str, str]:
    """Generate answers to common application form questions.

    Returns dict mapping question text -> answer string.
    Fixed answers (visa, salary, start date, leaving) are returned directly
    without calling the LLM. All other answers are generated from life-story.md.
    """
    if not check_ollama_available():
        logger.error("Ollama not available")
        return {}

    key_tech = ", ".join(job_analysis.get("key_technologies", [])[:3])

    # Build the LLM-generated question list with substitutions
    generated_questions = []
    for q in GENERATED_QUESTIONS:
        q = q.replace("{company}", company)
        q = q.replace("{tech}", key_tech or "the core technologies in this role")
        generated_questions.append(q)

    # Add "5 years" to the generated list
    five_years_q = "Where do you see yourself in 5 years?"
    generated_questions.append(five_years_q)

    questions_text = "\n".join(
        f"{i + 1}. {q}" for i, q in enumerate(generated_questions)
    )

    user_name = _extract_user_name(life_story)

    prompt = f"""Generate answers to these job application form questions for {user_name}.

ROLE: {title} at {company}
DOMAIN: {job_analysis.get('domain', 'data_analytics_bi')}
KEY TECHNOLOGIES: {key_tech}
COMPANY MISSION: {job_analysis.get('company_mission', '')}
SENIORITY: {job_analysis.get('seniority', 'director')}

JOB DESCRIPTION (excerpt):
{description[:1500]}

{user_name.upper()}'S BACKGROUND:
{life_story[:4000]}

QUESTIONS:
{questions_text}

{ANSWER_WRITING_RULES}

ADDITIONAL RULES:
- Reference specific projects, metrics, and technologies from {user_name}'s background.
- Do NOT fabricate metrics, roles, or technologies not present in the background above.
- For the "5 years" question: describe the kind of organizational impact and scope
  she wants to have — grounded in the trajectory visible in her background.
- For the "experience with [tech]" question: if the technology is not in her
  background, say so directly and connect the closest relevant experience instead.
- For the company-specific question: reference what is known about the company
  from the job description. Do not invent details about the company.

Return a JSON object where keys are question numbers as strings ("1" through "{len(generated_questions)}")
and values are the answer strings.
"""

    result = generate_structured(prompt, model=model, max_tokens=3000)

    if not result:
        logger.error("LLM returned no result for form answers.")
        return _fixed_answers_only(company)

    # Map generated answers back to question text
    answers: Dict[str, str] = {}
    for i, q in enumerate(generated_questions):
        key = str(i + 1)
        if key in result and result[key]:
            answers[q] = result[key]
        else:
            logger.warning("No answer generated for question %s: %s", key, q)

    # Inject fixed answers — these override any LLM attempts
    for question, answer in FIXED_QUESTIONS.items():
        if answer is not None:
            answers[question] = answer

    logger.info(
        "Generated %d form answers for %s at %s (%d fixed, %d LLM-generated)",
        len(answers),
        title,
        company,
        len([a for a in FIXED_QUESTIONS.values() if a is not None]),
        len([q for q in generated_questions if q in answers]),
    )

    return answers


def _extract_user_name(life_story: str) -> str:
    """Extract the user's full name from the life story."""
    import re
    for line in life_story.splitlines()[:20]:
        m = re.search(r'\*\*Full Name:\*\*\s*(.+)', line)
        if m:
            return m.group(1).strip()
    m = re.search(r'^#\s+Life Story\s*[—–-]\s*(.+)', life_story, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return "the candidate"


def _fixed_answers_only(company: str) -> Dict[str, str]:
    """Return only the fixed answers when LLM generation fails entirely."""
    return {
        q: a
        for q, a in FIXED_QUESTIONS.items()
        if a is not None
    }


def format_answers_for_display(answers: Dict[str, str]) -> str:
    """Format answers as a readable plain-text fill guide."""
    lines = []
    for question, answer in answers.items():
        lines.append(f"Q: {question}")
        lines.append(f"A: {answer}")
        lines.append("")
    return "\n".join(lines).strip()