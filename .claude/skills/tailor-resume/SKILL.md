---
name: tailor-resume
description: Tailor profile/resume.md into a targeted resume for one specific job posting -- reorders and rephrases existing content to mirror the posting's language without inventing experience. Use when the user asks to tailor, customize, or adapt their resume for a job, company, or posting.
---

# Tailor resume

Turns the master resume at `profile/resume.md` into a version aimed at one
specific posting. Output is markdown, saved next to the master so it is easy
to diff against.

## 1. Resolve the job posting

The argument passed to this skill can be any of:
- a jobhunt job id (16 hex chars) -> call `mcp__jobhunt__get_job`
- a company + role name that's likely in the tracker -> `mcp__jobhunt__search_jobs`
  to find the id, then `get_job`
- a URL -> WebFetch it
- pasted job description text -> use as-is

If nothing usable was given, ask which job (or offer the current shortlist
via `shortlist_for_review`).

Resolve this in one pass -- don't round-trip with the user if the input is
already unambiguous.

## 2. Read the source of truth

Read `profile/resume.md`. This is the only content allowed in the output --
never invent employers, titles, dates, numbers, or skills that aren't already
there. If it doesn't exist yet (gitignored, so a fresh clone won't have it),
stop and tell the user to `cp profile/resume.example.md profile/resume.md`
and fill it in first.

## 3. Tailor

From the posting, identify: the role title, the 5-10 keywords/technologies it
actually cares about, seniority level, and domain framing (e.g. "platform"
vs. "product").

Allowed edits (all reversible, all truthful):
- Reorder `Technical Skills` categories/items so what the posting cares about
  leads.
- Reorder or trim `Experience` bullets within each role so the most relevant
  ones lead; cut bullets that add nothing for this posting if the resume is
  running long.
- Rewrite bullet phrasing to use the posting's own terms for the same real
  work (e.g. "agent orchestration" vs "tool calling" -- same fact, different
  words), never to claim a new fact.
- Rewrite the `Summary` paragraph to foreground the experience most relevant
  to this posting.
- Adjust the title line under the name if a truthful synonym matches the
  posting better (e.g. "Platform Engineer" vs "Backend Software Engineer").

Never touch: employer names, job titles actually held, dates, degree, GPA, or
any metric/number. If the posting wants a skill or years of experience the
resume doesn't support, leave it out rather than adding it.

## 4. Write the output

Save to `profile/tailored/<company-slug>-<role-slug>.md`, using the same
section structure and style as `profile/resume.md` so it's a quick visual
diff. Create `profile/tailored/` if it doesn't exist -- it's gitignored,
under the same "never commit a real resume" rule as `profile/resume.md`.

Then reply with a short mapping, not the full document: 3-6 lines of
"posting wants X -> emphasized/reworded Y" so the user can sanity-check the
changes without rereading the whole resume.

## Notes

- One read of `resume.md`, one fetch of the posting, one write of the output
  -- don't loop or spawn a subagent for what is a single-file rewrite.
- This produces markdown only. PDF export is whatever process the user
  already uses for `profile/resume.md` -> `Resume.pdf`; this skill doesn't
  assume or install a renderer.
- If asked to tailor for several postings in one go, repeat steps 1-4 per
  posting rather than trying to produce one resume that fits all of them.
