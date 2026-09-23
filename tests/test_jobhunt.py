"""Tests for normalization, storage, and the triage heuristic.

Everything here runs offline against fixtures shaped like real API responses.
The tests marked ``live`` hit the actual public endpoints and are skipped unless
you ask for them: ``pytest -m live``.
"""

from __future__ import annotations

import httpx
import pytest

from jobhunt import config
from jobhunt.db import Store
from jobhunt.models import Job, html_to_text, looks_remote
from jobhunt.scoring import relevance
from jobhunt.sources import discover, greenhouse, smartrecruiters
from jobhunt.sources.lever import _iso_from_ms


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


@pytest.mark.live
async def test_smartrecruiters_endpoint_still_returns_expected_shape():
    from jobhunt.sources.base import make_client
    from jobhunt.sources.smartrecruiters import fetch_description, probe_board

    async with make_client() as client:
        count, titles = await probe_board(client, "ServiceNow")
        assert count > 0 and titles
        # One posting is enough to check the detail shape; the full board is pages.
        resp = await client.get(
            smartrecruiters.BASE.format(slug="ServiceNow"), params={"limit": 1}
        )
        job = smartrecruiters._to_job(resp.json()["content"][0], "ServiceNow", "ServiceNow")
        assert job.title and job.posted_at
        text = await fetch_description(client, job.source_id)
    assert text, "the detail endpoint should carry the posting text"


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


# ------------------------------------------------------------------ dates


def test_search_posted_since_filters_on_employer_date(store):
    store.upsert_jobs([
        make_job(source_id="1", title="Fresh", posted_at="2026-09-20T10:00:00-04:00"),
        make_job(source_id="2", title="Stale", posted_at="2026-03-01T10:00:00Z"),
    ])
    titles = [r["title"] for r in store.search(posted_since="2026-09-01")]
    assert titles == ["Fresh"]


def test_search_posted_since_falls_back_to_first_seen_for_free_text_dates(store):
    # A manual posting's "posted" is whatever the model typed. It shouldn't be
    # string-compared against an ISO cutoff, nor silently dropped.
    job = make_job(source="manual", posted_at="3 days ago")
    store.upsert_jobs([job])
    assert len(store.search(posted_since="2000-01-01")) == 1
    assert len(store.search(posted_since="2999-01-01")) == 0


def test_search_new_since_filters_on_first_seen(store):
    store.upsert_jobs([
        make_job(source_id="1", title="Old", first_seen="2026-01-01T00:00:00+00:00"),
        make_job(source_id="2", title="New"),
    ])
    titles = [r["title"] for r in store.search(new_since="2026-06-01")]
    assert titles == ["New"]


def test_upsert_refreshes_posted_at(store):
    # A source's posted date is authoritative, so a corrected value from a
    # later sync (e.g. an adapter fix) has to overwrite the stored one.
    store.upsert_jobs([make_job(posted_at="1700000000000")])
    store.upsert_jobs([make_job(posted_at="2023-11-14T22:13:20+00:00")])
    assert store.get_job(make_job().id)["posted_at"] == "2023-11-14T22:13:20+00:00"


def test_retire_unseen_only_touches_one_source_and_old_sightings(store):
    store.upsert_jobs([
        make_job(source="remoteok", source_id="1"),
        make_job(source="himalayas", source_id="2"),
    ])
    store.conn.execute("UPDATE jobs SET last_seen = '2026-01-01T00:00:00+00:00'")
    store.upsert_jobs([make_job(source="remoteok", source_id="3")])  # seen now

    assert store.retire_unseen("remoteok", "2026-06-01T00:00:00+00:00") == 1
    active = {(r["source"], r["source_id"]) for r in store.search()}
    assert active == {("himalayas", "2"), ("remoteok", "3")}


async def test_greenhouse_prefers_first_published_over_updated_at():
    payload = {"jobs": [{
        "id": 1, "title": "Backend Engineer", "absolute_url": "https://x/1",
        "location": {"name": "Remote"}, "content": "",
        "first_published": "2026-03-01T00:00:00-04:00",
        "updated_at": "2026-09-20T00:00:00-04:00",
    }]}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))

    async with httpx.AsyncClient(transport=transport) as client:
        [job] = await greenhouse.fetch_board(client, "acme")
    assert job.posted_at == "2026-03-01T00:00:00-04:00"


def test_lever_converts_epoch_ms_to_iso():
    assert _iso_from_ms(1700000000000) == "2023-11-14T22:13:20+00:00"
    assert _iso_from_ms(None) == ""


# ------------------------------------------------------------ board discovery


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://job-boards.greenhouse.io/anthropic/jobs/402", [("greenhouse", "anthropic")]),
        ("https://boards.greenhouse.io/embed/job_board?for=stripe", [("greenhouse", "stripe")]),
        ("https://jobs.ashbyhq.com/deepgram/1395ef4d", [("ashby", "deepgram")]),
        ("https://jobs.lever.co/curai/a5e85c45-912f", [("lever", "curai")]),
        ("https://jobs.smartrecruiters.com/ServiceNow/1", [("smartrecruiters", "ServiceNow")]),
        ("https://builtin.com/job/ai-engineer/10949758", []),
    ],
)
def test_slugs_from_text(url, expected):
    assert discover.slugs_from_text(url) == expected


def test_guess_slugs_tries_joined_hyphenated_and_suffixless_forms():
    assert discover.guess_slugs("Together AI") == ["togetherai", "together-ai", "together"]
    assert discover.guess_slugs("Monte Carlo") == ["montecarlo", "monte-carlo", "monte"]
    assert discover.guess_slugs("") == []


def _ats_transport(live: dict[str, object]) -> httpx.MockTransport:
    """Answer ATS API requests from ``live`` (url substring -> JSON body), 404 otherwise."""
    def handler(request: httpx.Request) -> httpx.Response:
        for needle, body in live.items():
            if needle in str(request.url):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"ok": False})
    return httpx.MockTransport(handler)


async def test_find_boards_by_name_returns_only_live_boards_with_evidence():
    transport = _ats_transport({
        "posting-api/job-board/deepgram": {"jobs": [{"id": "a", "title": "ML Engineer"}]},
    })
    async with httpx.AsyncClient(transport=transport) as client:
        result = await discover.find_boards(client, company="Deepgram", delay=0)
    assert [(m.source, m.slug, m.job_count) for m in result.matches] == [("ashby", "deepgram", 1)]
    assert result.matches[0].sample_titles == ["ML Engineer"]
    assert "greenhouse:deepgram" in result.tried
    assert result.errors == {}


async def test_find_boards_prefers_url_over_name_guesses():
    transport = _ats_transport({"boards/acme-corp/jobs": {"jobs": []}})
    async with httpx.AsyncClient(transport=transport) as client:
        result = await discover.find_boards(
            client, company="Acme", url="https://job-boards.greenhouse.io/acme-corp", delay=0
        )
    assert result.tried == ["greenhouse:acme-corp"]
    assert [m.slug for m in result.matches] == ["acme-corp"]


async def test_probe_reports_server_errors_instead_of_calling_the_slug_wrong():
    transport = httpx.MockTransport(lambda req: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await discover.find_boards(client, company="Acme", delay=0)
    assert result.matches == []
    assert result.errors["greenhouse:acme"] == "HTTP 503"


# ------------------------------------------------------------ smartrecruiters


def _sr_posting(i: int, **loc) -> dict:
    return {
        "id": str(i), "name": f"Engineer {i}", "releasedDate": "2026-09-20T00:00:00.000Z",
        "location": {"fullLocation": "Austin, TX, United States", **loc},
        "department": {"label": "Engineering"},
    }


def _sr_transport(total: int, detail: dict | None = None) -> httpx.MockTransport:
    """A paged SmartRecruiters list of ``total`` postings, plus one detail body."""
    postings = [_sr_posting(i) for i in range(total)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") > 4:  # /v1/companies/{slug}/postings/{id}
            return httpx.Response(200, json=detail or {})
        limit = int(request.url.params.get("limit", 100))
        offset = int(request.url.params.get("offset", 0))
        page = postings[offset:offset + limit]
        return httpx.Response(200, json={"totalFound": total, "content": page})
    return httpx.MockTransport(handler)


async def test_smartrecruiters_pages_through_the_whole_board():
    async with httpx.AsyncClient(transport=_sr_transport(250)) as client:
        jobs = await smartrecruiters.fetch_board(client, "Acme", "Acme Corp", delay=0)
    assert len(jobs) == 250
    assert len({j.id for j in jobs}) == 250
    job = jobs[0]
    assert (job.company, job.source_id) == ("Acme Corp", "Acme/0")
    assert job.url == "https://jobs.smartrecruiters.com/Acme/0"
    assert job.description == ""  # fetched on first read, not during sync


async def test_smartrecruiters_empty_board_is_an_error_not_a_quiet_success():
    # Unknown companies return 200 + an empty list, indistinguishable from a
    # real board with nothing open. Neither should count as "fetched".
    async with httpx.AsyncClient(transport=_sr_transport(0)) as client:
        result = await smartrecruiters.sync(client, {"Nope": "Nope"}, delay=0)
    assert result.fetched == []
    assert "Nope" in result.errors


@pytest.mark.parametrize(
    "loc, remote",
    [({"remote": True}, True), ({"remote": True, "hybrid": True}, False), ({}, False)],
)
def test_smartrecruiters_remote_flag(loc, remote):
    job = smartrecruiters._to_job(_sr_posting(1, **loc), "Acme", "Acme")
    assert job.remote is remote


async def test_smartrecruiters_description_joins_sections_in_reading_order():
    detail = {"jobAd": {"sections": {
        "companyDescription": {"title": "Company", "text": "<p>We make things.</p>"},
        "jobDescription": {"title": "The role", "text": "<p>Build&#xa0;APIs.</p>"},
        "qualifications": {"title": "You have", "text": "<ul><li>Python</li></ul>"},
    }}}
    async with httpx.AsyncClient(transport=_sr_transport(1, detail)) as client:
        text = await smartrecruiters.fetch_description(client, "Acme/0")
    assert text.index("The role") < text.index("You have") < text.index("Company")
    assert "Build APIs." in text
    assert "- Python" in text


async def test_discovery_probes_smartrecruiters_with_one_request():
    calls = []
    inner = _sr_transport(5000)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return inner.handle_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        match = await discover.probe(client, "smartrecruiters", "Acme")
    assert match is not None and match.job_count == 5000
    assert len(calls) == 1


def test_resync_keeps_a_description_fetched_on_demand(store):
    job = make_job(source="smartrecruiters", source_id="Acme/1", description="")
    store.upsert_jobs([job])
    store.set_description(job.id, "Fetched on first read.")
    store.upsert_jobs([job])  # the list feed again, still without a description
    assert store.get_job(job.id)["description"] == "Fetched on first read."


def test_html_to_text_decodes_hex_entities():
    assert html_to_text("a&#xa0;b&#x2014;c") == "a b—c"
