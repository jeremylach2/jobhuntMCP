---
name: triage-jobs
description: Sync job boards and produce a ranked shortlist of top-fit postings. Use when asked to sync jobs, find/shortlist top jobs, triage new postings, "what's new that's worth looking at," or to search all jobs / the whole market for a role or topic beyond the watched companies.
---

# Triage jobs

The full recipe for turning a raw sync into a ranked, scored shortlist. Follow
it in order -- each step depends on state the previous one wrote. Do not skip
steps because a shortcut looks obviously fine; the whole point of writing this
down is that the judgment calls below (what counts as a fit, what counts as a
red flag) aren't visible from the tool names alone.

## 1. Sync

`mcp__jobhunt__sync_boards` with default sources (greenhouse, ashby, lever,
smartrecruiters) -- the target-company boards. Only add
`himalayas`/`hn`/`remoteok` (the wider, keyword-scoped market) if the user
asks for broader coverage than the target list, since those pull in a much
larger and noisier set of postings.

This can take a couple of minutes for ~90 boards and may run as a background
task. Wait for it to finish before shortlisting -- scoring against a stale or
half-synced DB defeats the point.

### Searching all jobs (beyond the target list)

If the user asks to search "all jobs", "the whole market", or for a specific
role/topic ("anything in Rust", "data engineer roles anywhere"), use
`mcp__jobhunt__search_market(query=...)` instead of, or alongside, the sync.
`search_jobs` and `shortlist_for_review` only see what's already stored:
the watched companies, plus aggregator postings that matched the *profile*
keywords. `search_market` asks the aggregators live for the user's query, so
it finds roles at companies that aren't on the target list.

- Sources: `himalayas` (whole remote catalog, searched server-side), `hn`
  (this month's Who-is-hiring thread, full-text searched), `remoteok`
  (only its ~100 newest postings). Default is all three.
- Use a short query: one role or technology ("platform engineer",
  "kubernetes"). For several topics, make several calls instead of one long
  query. `hn` and `remoteok` match the query as plain text, so a long query
  matches almost nothing.
- Results are saved, so the returned ids work with `get_job` /
  `record_fit` / `set_status` right away, and `search_jobs(query=...)`
  finds them again later. Saved results are never marked closed, so a
  posting from an old search can stay in the database after the job
  itself is gone.
- **Triage the returned ids directly** (step 4 onward), not through
  `shortlist_for_review`. The shortlist ranks by the profile keywords, so
  it can bury exactly the off-profile postings the user asked for. Read
  the most promising-looking lines with `get_job` before scoring. Results
  come back in source order, not ranked. A `[+N more location(s), same
  role]` tag means the same company+title was also posted for other
  countries. Only one id is shown; `search_jobs(company=...)` lists the
  rest if the shown location doesn't suit the user.
- Leave `countries` on `"auto"`. It reads `market.countries` from
  targets.yaml, so Himalayas only returns postings that hire from the
  user's country (worldwide-open ones included). Pass `"any"` only if the
  user asks for roles elsewhere. An `Errors: himalayas[X] (HTTP 400)` line
  means Himalayas didn't recognize country X: retry with the full English
  name or ISO code.
- The country filter covers Himalayas only. HN and RemoteOK locations are
  free text, so check those against the profile when you read each posting.
  Most results are remote (Himalayas and RemoteOK are remote-only), but HN
  postings vary.
- HN titles/companies come from free-form first lines and are often messy
  (e.g. the company's URL as the title). Judge from the full text, not the
  one-line summary.
- Never LinkedIn/Indeed/Glassdoor, even if "all jobs" seems to imply them.
  Those stay manual by design. Say so if the user expects them.

## 2. Read the profile once

`mcp__jobhunt__get_profile` before scoring anything. It has the resume, the
target keywords, the de-prioritize list, and stated preferences (remote,
location, level). Fit judgments made without reading this are guesses.

## 3. Shortlist

`mcp__jobhunt__shortlist_for_review`. Leave `remote_only` on its default,
`"auto"` -- it already reads the profile's `preferences.remote` and restricts
to remote postings when that's `"required"`, so you don't need to duplicate
that check yourself. The number printed as `relevance=NN` on each line is a
keyword prefilter score, not a fit judgment -- it only means "plausibly the
right family of role." Never present it to the user as a quality or fit score,
and never rank or filter your final recommendations by it. Read every
candidate properly (step 4) before deciding anything.

**Don't trust the `[remote]` tag as final.** It's a heuristic over
location/title/description text. It catches most in-office requirements now,
but a posting can still say "remote-first" up top and bury a hybrid or
in-office expectation somewhere in the body. Confirm remote status when you
read the full posting in step 4, not just from the tag in this list.

## 4. Read and score every candidate

For each posting in the shortlist:

1. `mcp__jobhunt__get_job` -- read the full description. This is also where
   you'll catch a bad `[remote]` tag, a seniority mismatch the title didn't
   show, or a stack gap.
2. `mcp__jobhunt__get_company_notes` for that company. This is a **hard
   filter, not a tiebreaker**: if cached notes describe a toxic culture,
   chronic crunch, or a "burn and churn" reputation, that screens the posting
   out regardless of how well it otherwise matches -- score it low and say
   why. If there are no cached notes, don't do fresh research yourself unless
   asked (it's expensive and the server deliberately doesn't automate it) --
   just don't claim work-life balance is fine when it's actually unknown.
   Write "WLB unknown, not researched" into `concerns` for anything you'd
   otherwise rank highly, so the gap is visible later.
3. `mcp__jobhunt__record_fit` -- score every candidate you looked at,
   including the ones you're screening out (frontend-only roles, wrong
   country/timezone, wrong level, WLB red flag). A low score with a one-line
   reason is what makes the screen-out durable across sessions; skipping
   `record_fit` on a reject means the next sync re-surfaces it as unscored.

## 5. Report

Give the user a ranked list of the real top picks (aim for 8-12), each with
company, title, your score (not the prefilter number), a one-line verdict,
comp if known, and the job id. Separately mention anything screened out for a
reason worth knowing (e.g. "great fit but wrong country" or "flagged for
WLB") -- that's often more useful to a job search than silence.

## 6. Tailor + PDF (optional -- only if the user asks)

Don't do this automatically for every posting in the shortlist; it's a
per-job step for whichever posting(s) the user decides to actually apply to.

1. Invoke the `tailor-resume` skill with the job id, which writes
   `profile/tailored/<company-slug>-<role-slug>.md`.
2. If the user also wants a PDF, run `resume_to_pdf.py` (repo root) against
   that file, e.g.:
   `python resume_to_pdf.py profile/tailored/<company-slug>-<role-slug>.md`.
   It writes a same-named `.pdf` next to the input unless a second argument
   gives an explicit output path. Uses the `pdf` optional dependency group
   (`markdown` + `xhtml2pdf`) -- if the import fails, `uv pip install -e ".[pdf]"`
   before retrying. Deliberately not WeasyPrint: that needs system-level
   GTK/Pango/Cairo libraries pip can't install, which fails outright on a
   plain Windows setup; `xhtml2pdf` is pure Python and needs nothing extra.
