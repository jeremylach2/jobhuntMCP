"""Tests for normalization, storage, and the triage heuristic.

Everything here runs offline against fixtures shaped like real API responses.
The tests marked ``live`` hit the actual public endpoints and are skipped unless
you ask for them: ``pytest -m live``.
"""

from __future__ import annotations

import pytest

from jobhunt import config
from jobhunt.db import Store
from jobhunt.models import Job, html_to_text, looks_remote
from jobhunt.scoring import relevance


def make_job(**kwargs) -> Job:
    base = dict(
        source="greenhouse",
        source_id="1",
        company="Acme",
        title="Backend Engineer",
        url="https://example.com/1",
    )
    return Job(**{**base, **kwargs})


# ------------------------------------------------------------------ normalizing


def test_html_to_text_keeps_bullets_on_separate_lines():
    html = "<p>Intro</p><ul><li>First</li><li>Second</li></ul>"
    text = html_to_text(html)
    assert "- First" in text
    assert "- Second" in text
    # The bullets must not run together into one line.
    assert "FirstSecond" not in text.replace(" ", "")


def test_html_to_text_decodes_entities():
    assert html_to_text("<p>R&amp;D &mdash; 10&#37;</p>") == "R&D - 10%"


def test_html_to_text_handles_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_job_id_is_stable_and_source_scoped():
    a = make_job()
    b = make_job(title="Totally Different Title")
    assert a.id == b.id, "same source+source_id must be the same posting"

    c = make_job(source="lever")
    assert a.id != c.id, "the same id on a different board is a different posting"


def test_preferences_defaults_to_empty_dict(tmp_path):
    cfg = config.load(root=tmp_path)
    assert cfg.preferences == {}


def test_preferences_reads_targets_yaml(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "targets.yaml").write_text(
        "preferences:\n"
        "  locations: [Chicago, IL]\n"
        "  remote: preferred\n"
        "  min_salary: '150000'\n",
        encoding="utf-8",
    )
    cfg = config.load(root=tmp_path)
    assert cfg.preferences["remote"] == "preferred"
    assert cfg.preferences["min_salary"] == "150000"


@pytest.mark.parametrize(
    "fields,expected",
    [
        (("Remote - US",), True),
        (("Anywhere",), True),
        (("New York, NY",), False),
        # "hybrid" wins over "remote": the role still requires relocation.
        (("Remote / Hybrid - SF",), False),
        (("San Francisco (on-site)", "Remote Engineer"), False),
        # An in-office cadence stated only in the description, with nothing
        # disqualifying in location/title, should still be caught.
        (
            ("Remote - US", "Senior Engineer", "Team is expected 4 days a week in the office."),
            False,
        ),
        (
            ("Remote", "Senior Engineer", "Onsite 3 days/week at our SF HQ is required."),
            False,
        ),
        # A stray day-count with no office language shouldn't trip the check.
        (("Remote - US", "Senior Engineer", "On-call rotation is 7 days a week."), True),
    ],
)
def test_looks_remote(fields, expected):
    assert looks_remote(*fields) is expected


# --------------------------------------------------------------------- storage


# ------------------------------------------------------------------ remoteok


def test_remoteok_skips_legal_notice_and_filters_by_keyword():
    from jobhunt.sources.remoteok import _matches

    legal_notice = {"legal": "API Terms of Service..."}
    assert "id" not in legal_notice  # the sync loop's skip condition

    job = {"position": "Backend Engineer", "tags": ["python", "kafka"]}
    assert _matches(job, "", ["python"]) is True
    assert _matches(job, "", ["frontend"]) is False
    assert _matches(job, "", []) is True


def test_remoteok_salary_formats_range_and_single_value():
    from jobhunt.sources.remoteok import _salary

    assert _salary({"salary_min": 70000, "salary_max": 90000}) == "USD 70,000 - 90,000"
    assert _salary({"salary_min": 70000, "salary_max": 0}) == "USD 70,000"
    assert _salary({"salary_min": 0, "salary_max": 0}) == ""


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def test_upsert_counts_new_once_and_preserves_first_seen(store):
    job = make_job()
    new, seen = store.upsert_jobs([job])
    assert (new, seen) == (1, 1)
    original_first_seen = store.get_job(job.id)["first_seen"]

    # Re-syncing the same posting with an updated title is an update, not a new row.
    new, seen = store.upsert_jobs([make_job(title="Backend Engineer II")])
    assert (new, seen) == (0, 1)

    row = store.get_job(job.id)
    assert row["title"] == "Backend Engineer II"
    assert row["first_seen"] == original_first_seen


def test_deactivate_missing_only_touches_synced_companies(store):
    acme = make_job(source_id="1", company="Acme")
    other = make_job(source_id="2", company="Globex")
    store.upsert_jobs([acme, other])

    # A sync that only reached Acme must not retire Globex's posting.
    retired = store.deactivate_missing("greenhouse", ["Acme"], keep_ids=set())
    assert retired == 1
    assert store.get_job(acme.id)["active"] == 0
    assert store.get_job(other.id)["active"] == 1


def test_deactivate_missing_is_a_noop_when_nothing_was_fetched(store):
    job = make_job()
    store.upsert_jobs([job])
    # A fully failed sync reports no companies. Nothing should be retired.
    assert store.deactivate_missing("greenhouse", [], keep_ids=set()) == 0
    assert store.get_job(job.id)["active"] == 1


def test_search_filters(store):
    store.upsert_jobs([
        make_job(source_id="1", title="Backend Engineer", remote=True),
        make_job(source_id="2", title="Sales Director", remote=False),
        make_job(source_id="3", title="Platform Engineer", company="Globex"),
    ])
    assert len(store.search()) == 3
    assert len(store.search(remote_only=True)) == 1
    assert len(store.search(query="Engineer")) == 2
    assert len(store.search(company="Globex")) == 1
    assert len(store.search(source="lever")) == 0


def test_scored_jobs_sort_above_unscored(store):
    low = make_job(source_id="1", title="Low")
    high = make_job(source_id="2", title="High")
    unscored = make_job(source_id="3", title="Unscored")
    store.upsert_jobs([low, high, unscored])
    store.set_fit(low.id, 30)
    store.set_fit(high.id, 90)

    titles = [r["title"] for r in store.search()]
    assert titles[:2] == ["High", "Low"]
    assert titles[2] == "Unscored"


def test_set_fit_is_idempotent_and_overwrites(store):
    job = make_job()
    store.upsert_jobs([job])
    store.set_fit(job.id, 50, "maybe", ["a"], ["b"])
    store.set_fit(job.id, 85, "yes", ["strong match"], [])

    row = store.get_job(job.id)
    assert row["score"] == 85
    assert row["verdict"] == "yes"
    assert "strong match" in row["reasons"]


def test_status_transitions_preserve_applied_date(store):
    job = make_job()
    store.upsert_jobs([job])
    store.set_status(job.id, "applied", "submitted via careers page")
    applied_at = store.get_job(job.id)["applied_at"]
    assert applied_at

    store.set_status(job.id, "screen")
    row = store.get_job(job.id)
    assert row["status"] == "screen"
    assert row["applied_at"] == applied_at, "applied date must survive later transitions"

    # Both transitions are in the history.
    kinds = [e["detail"].split(":")[0] for e in store.events(job.id)]
    assert kinds == ["applied", "screen"]


def test_unknown_status_rejected(store):
    job = make_job()
    store.upsert_jobs([job])
    with pytest.raises(ValueError, match="unknown status"):
        store.set_status(job.id, "hired")


def test_stats_counts(store):
    job = make_job()
    store.upsert_jobs([job, make_job(source_id="2")])
    store.set_fit(job.id, 70)
    store.set_status(job.id, "applied")

    s = store.stats()
    assert s["active_jobs"] == 2
    assert s["scored"] == 1
    assert s["by_status"] == {"applied": 1}
    assert s["by_source"] == {"greenhouse": 2}


# --------------------------------------------------------------------- triage


def test_relevance_rejects_wrong_function_outright():
    assert relevance("Enterprise Account Executive").score == 0
    assert relevance("Senior Recruiter, Technical").score == 0


def test_relevance_rejects_internships():
    result = relevance("Software Engineering Intern")
    assert result.score == 0
    assert "too junior" in result.flags


def test_relevance_flags_but_does_not_drop_senior_titles():
    result = relevance("Staff Software Engineer, AI Platform")
    assert "likely too senior" in result.flags
    assert result.score > 0, "over-level roles are worth a look, just penalized"


def test_relevance_ranks_agent_infra_above_generic_backend():
    agent = relevance(
        "Software Engineer, Agent Infrastructure",
        "Build MCP tooling and LLM agent orchestration in Python.",
    )
    generic = relevance(
        "Software Engineer",
        "Work on our web application.",
    )
    assert agent.score > generic.score


def test_relevance_weights_title_above_description():
    in_title = relevance("Backend Engineer, Kafka Streaming Platform")
    in_body = relevance("Software Engineer", "Our stack happens to include Kafka.")
    assert in_title.score > in_body.score


def test_relevance_never_leaves_the_0_100_range():
    loaded = relevance(
        "Senior Backend Engineer, AI Agent Platform",
        "MCP, LLM, RAG, inference, Python, Kotlin, Kafka, AWS, Kubernetes, "
        "Snowflake, MongoDB, distributed systems, event-driven, API design",
        extra_keywords=["agent", "backend", "platform", "MCP", "LLM"],
    )
    assert 0 <= loaded.score <= 100


# ----------------------------------------------------------------------- live


@pytest.mark.live
async def test_greenhouse_endpoint_still_returns_expected_shape():
    from jobhunt.sources.base import make_client
    from jobhunt.sources.greenhouse import fetch_board

    async with make_client() as client:
        jobs = await fetch_board(client, "anthropic", "Anthropic")
    assert jobs, "Anthropic's board should never be empty"
    assert all(j.url and j.title for j in jobs)


@pytest.mark.live
async def test_ashby_endpoint_still_returns_expected_shape():
    from jobhunt.sources.ashby import fetch_board
    from jobhunt.sources.base import make_client

    async with make_client() as client:
        jobs = await fetch_board(client, "ramp", "Ramp")
    assert jobs
    assert any(j.compensation for j in jobs), "Ashby should expose salary bands"


@pytest.mark.live
async def test_remoteok_endpoint_still_returns_expected_shape():
    from jobhunt.sources.base import make_client
    from jobhunt.sources.remoteok import sync

    async with make_client() as client:
        result = await sync(client, keywords=[])
    assert not result.errors
    assert result.jobs, "the firehose should never be empty"
    assert all(j.url and j.title and j.remote for j in result.jobs)


def test_exists(store):
    job = make_job()
    store.upsert_jobs([job])
    assert store.exists(job.id) is True
    assert store.exists("no-such-id") is False


def test_search_description_limit_truncates_but_still_filters_on_full_text(store):
    store.upsert_jobs([make_job(description="x" * 5000 + "needle")])
    rows = store.search(description_limit=10)
    assert len(rows[0]["description"]) == 10

    # The truncated projection must not affect what `query` can match.
    assert len(store.search(query="needle", description_limit=10)) == 1


def test_search_description_limit_zero_omits_description(store):
    store.upsert_jobs([make_job(description="some text")])
    row = store.search(description_limit=0)[0]
    assert row["description"] == ""


def test_search_limit_zero_returns_everything(store):
    store.upsert_jobs([make_job(source_id=str(i)) for i in range(60)])
    assert len(store.search(limit=10)) == 10
    assert len(store.search(limit=0)) == 60, "triage must see the whole corpus"


def test_triage_collapses_location_variants(store):
    from jobhunt.scoring import triage

    # The same role posted once per location, as large employers actually do.
    store.upsert_jobs([
        make_job(source_id=str(i), title="Backend Engineer, Platform", location=loc)
        for i, loc in enumerate(["NYC", "SF", "Remote", "Austin"])
    ])
    ranked = triage(store.search(limit=0))
    assert len(ranked) == 1, "one row per (company, title), not per location"


def test_triage_keeps_distinct_roles_at_one_company(store):
    from jobhunt.scoring import triage

    store.upsert_jobs([
        make_job(source_id="1", title="Backend Engineer, Platform"),
        make_job(source_id="2", title="Backend Engineer, Inference"),
    ])
    assert len(triage(store.search(limit=0))) == 2


# ----------------------------------------------------------- company notes


def test_company_notes_round_trip(store):
    assert store.get_company_notes("Acme") is None

    store.set_company_notes("Acme", "Good WLB per recent reviews; no layoffs in 2 years.")
    row = store.get_company_notes("Acme")
    assert "Good WLB" in row["notes"]
    assert row["updated_at"]


def test_company_notes_lookup_is_case_insensitive(store):
    store.set_company_notes("Acme Corp", "hybrid, 3 days in office")
    assert store.get_company_notes("acme corp")["notes"] == "hybrid, 3 days in office"


def test_company_notes_set_overwrites_not_appends(store):
    store.set_company_notes("Acme", "first pass: looks fine")
    store.set_company_notes("Acme", "updated: layoffs announced last week")

    row = store.get_company_notes("Acme")
    assert row["notes"] == "updated: layoffs announced last week"
    assert "first pass" not in row["notes"]


# ------------------------------------------------------------- thread safety


def test_store_is_usable_from_multiple_threads(store):
    """A Store must survive being called from a pool of worker threads.

    The MCP server runs sync tool functions on a thread pool, so one Store
    gets reached from whichever thread the framework picks. A single sqlite3
    connection is bound to its creating thread and raises ProgrammingError
    anywhere else, which surfaced as every tool failing intermittently until
    the server was restarted.
    """
    import threading

    store.upsert_jobs([make_job(source_id="thread-1")])
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for _ in range(5):
                store.search(limit=5)
                store.set_company_notes(f"Company{n}", "notes")
                assert store.get_company_notes(f"Company{n}") is not None
                store.stats()
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threaded access failed: {errors[:3]}"
