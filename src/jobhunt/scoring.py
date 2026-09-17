"""A cheap, deterministic prefilter that runs before any model judgment.

The point of this module is triage, not evaluation. A full sync pulls in tens
of thousands of postings, and sending all of them to a model to be read would
be slow and expensive, and most are trivially wrong (sales roles, VP roles,
internships). So this assigns a coarse 0-100 relevance number from string
matching alone, and the MCP server uses it to pick which few dozen postings are
worth a model actually reading.

Deliberately not a fit score. It cannot tell whether a role is interesting,
only whether it is plausibly in the right family. Real judgment happens in the
``fit`` table, written by the model after reading the posting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Weighted signals. Skills are grouped so that matching three Kafka-adjacent
# terms does not count triple against one match in an unrelated group.
SKILL_GROUPS: dict[str, tuple[str, ...]] = {
    "agents": ("mcp", "model context protocol", "agent", "agentic", "tool calling",
               "function calling", "llm", "rag", "retrieval augmented"),
    "ai_platform": ("ai platform", "ml platform", "inference", "model serving",
                    "foundation model", "applied ai", "genai", "generative ai"),
    "backend": ("backend", "back-end", "distributed systems", "microservice",
                "service architecture", "api design", "rest api", "grpc"),
    "languages": ("python", "kotlin", "java", "typescript", "golang", " go ", "scala"),
    "streaming": ("kafka", "event-driven", "event driven", "streaming", "pub/sub",
                  "sqs", "sns", "queue"),
    "cloud": ("aws", "bedrock", "kubernetes", "lambda", "terraform", "gcp", "azure"),
    "data": ("snowflake", "mongodb", "postgres", "data pipeline", "airflow",
             "dbt", "warehouse"),
}

# Title patterns that indicate the level is wrong for a mid-level IC with a few
# years of experience. Matched against the title only, where they are reliable.
TOO_SENIOR = re.compile(
    r"\b(staff|principal|distinguished|fellow|director|head of|vp|vice president|"
    r"chief|architect|manager|lead engineer|engineering lead)\b",
    re.I,
)
TOO_JUNIOR = re.compile(r"\b(intern|internship|new grad|graduate|apprentice|co-op)\b", re.I)

# Non-engineering functions. Cheap to exclude and a large share of ATS volume.
# Stems are spelled out (recruit\w* catches recruiter/recruiting) because word
# boundaries alone miss the inflected forms that actually appear in titles.
# Terms stay narrow on purpose: a bare "design" would discard "Engineer, Design
# Systems", and a bare "solutions" would discard "Solutions Platform Engineer".
WRONG_FUNCTION = re.compile(
    r"\b(sales|account executive|recruit\w*|marketing|designer|"
    r"customer success|support engineer|solutions (architect|engineer|consultant)|"
    r"field engineer|people ops|finance|legal|counsel|controller|"
    r"accountant|content \w+|community|partnerships|business development|"
    r"data entry|technician|hardware|mechanical|electrical|facilities|"
    r"chef|nurse|clinical|physician|therapist)\b",
    re.I,
)

ENGINEERING = re.compile(
    r"\b(engineer|engineering|developer|swe|sde|programmer|scientist)\b", re.I
)


@dataclass
class Relevance:
    score: int
    matched: list[str]
    flags: list[str]

    def as_dict(self) -> dict[str, object]:
        return {"relevance": self.score, "matched": self.matched, "flags": self.flags}


def relevance(
    title: str,
    description: str = "",
    department: str = "",
    extra_keywords: list[str] | None = None,
) -> Relevance:
    """Score 0-100 for "is this plausibly the right kind of job".

    Title carries more weight than description because descriptions mention
    every technology a company uses, while titles describe the actual role.
    """
    title_l = title.lower()
    # Descriptions run long and repetitive. The first 4000 characters cover the
    # role summary and requirements, which is where the signal is.
    body = f"{department} {description[:4000]}".lower()
    matched: list[str] = []
    flags: list[str] = []

    score = 0

    # Checked before the engineering bonus so that titles which are both
    # "Sales Engineer", "Support Engineer", "Solutions Architect" are
    # discarded rather than credited for the word "engineer".
    if WRONG_FUNCTION.search(title):
        flags.append("non-engineering title")
        return Relevance(0, [], flags)

    if TOO_JUNIOR.search(title):
        flags.append("too junior")
        return Relevance(0, [], flags)

    if ENGINEERING.search(title):
        score += 20

    if TOO_SENIOR.search(title):
        flags.append("likely too senior")
        score -= 25

    if re.search(r"\b(senior|sr\.?|ii|iii|mid)\b", title_l):
        score += 10

    for group, terms in SKILL_GROUPS.items():
        hit = next((t for t in terms if t in title_l), None)
        if hit:
            score += 12  # a skill named in the title is a strong signal
            matched.append(f"{group}:{hit.strip()} (title)")
            continue
        hit = next((t for t in terms if t in body), None)
        if hit:
            score += 5
            matched.append(f"{group}:{hit.strip()}")

    for kw in extra_keywords or []:
        if kw.lower() in title_l:
            score += 6
            matched.append(f"keyword:{kw} (title)")

    return Relevance(max(0, min(100, score)), matched, flags)


def triage(rows, keywords: list[str] | None = None) -> list[tuple[Relevance, object]]:
    """Rank unscored postings and collapse near-duplicates.

    Large employers post one row per location variant. The same ClickHouse
    role can appear five times with different ids. Those are genuinely distinct
    postings, but showing all of them crowds out other companies, so only the
    best-scoring variant of each (company, title) pair survives into the
    shortlist.
    """
    best: dict[tuple[str, str], tuple[Relevance, object]] = {}
    for row in rows:
        rel = relevance(
            row["title"], row["description"] or "", row["department"] or "", keywords
        )
        if rel.score <= 0:
            continue
        key = (row["company"].lower(), row["title"].strip().lower())
        if key not in best or rel.score > best[key][0].score:
            best[key] = (rel, row)
    ranked = list(best.values())
    ranked.sort(key=lambda pair: pair[0].score, reverse=True)
    return ranked
