---
name: setup
description: Set up a fresh jobhunt clone -- install dependencies, create profile/resume.md and profile/targets.yaml from the gitignored examples, verify ATS slugs, and register the MCP server with Claude Code. Use when the user asks to set up, configure, onboard, or initialize jobhunt, or when a tool reports a missing resume, empty target list, or the MCP server isn't registered yet.
---

# Set up jobhunt

Everything under `profile/` is gitignored on purpose (see AGENTS.md), so a
fresh clone has none of it: no resume, no target companies, no keywords. This
skill fills that in for one user, in order, without overwriting anything
that's already there. Check what already exists before each step -- this
should be safe to re-run if setup was left half-done.

## 1. Python environment

Check whether `.venv` exists and the package imports
(`.venv/Scripts/python.exe -c "import jobhunt"` on Windows). If not:

```bash
uv venv
uv pip install -e ".[dev]"
```

The venv has no `pip` preinstalled -- always `uv pip install`, per AGENTS.md.

## 2. Resume (`profile/resume.md`)

Skip this step entirely if the file already exists and has real content (not
just the template placeholders like "Your Name" / "you@example.com").

Otherwise:

1. `cp profile/resume.example.md profile/resume.md` to seed the structure.
2. Get the content from the user rather than inventing any of it. Ask if they
   have an existing resume to work from -- a file in the repo (check for
   something like `Resume.pdf` at the root), a path they give you, or pasted
   text. If they hand you a PDF or docx, read it and transcribe the real
   content into the markdown sections; don't summarize or drop specifics like
   dates and numbers.
3. If they have nothing existing, ask directly for what each section needs:
   current/target title, a one-paragraph summary of what they build and want
   next, technical skills grouped by category, experience bullets per role
   (title, company, dates, impact), education, and -- important, per the
   template's own note -- an explicit "what I'm looking for" (level, domain,
   remote vs. hybrid, company stage, anything disqualifying). Fit assessments
   lean heavily on this last section; don't let it come out generic.
4. Write the result with the Edit/Write tool. Never leave placeholder text
   from the template in the final file.

## 3. Targets (`profile/targets.yaml`)

Skip if the file already exists with at least one company under `companies`.

Otherwise:

1. `cp profile/targets.example.yaml profile/targets.yaml`.
2. Fill in `keywords` / `exclude_keywords` / `preferences` and the initial
   `companies` list by editing this file directly (Edit tool), so the
   explanatory comments already in the example survive. **Don't** reach for
   the `add_target` MCP tool for this initial batch -- it works by rewriting
   the whole file with `yaml.safe_dump`, which drops every comment in it.
   `add_target` is for adding one company later, after the comments are
   already gone from a save; it's the wrong tool for filling in the first
   batch.
3. Ask the user which companies to watch. For each one, get its careers page
   and figure out the ATS + slug from the URL shape:
   ```
   job-boards.greenhouse.io/SLUG   -> greenhouse
   jobs.ashbyhq.com/SLUG           -> ashby
   jobs.lever.co/SLUG              -> lever
   ```
   Then verify the slug against the live endpoint before writing it in --
   guessed slugs fail silently-ish (they land under `errors` in the sync
   report, easy to skim past):
   ```bash
   curl -s "https://boards-api.greenhouse.io/v1/boards/SLUG/jobs" | head -c 200
   curl -s "https://api.ashbyhq.com/posting-api/job-board/SLUG" | head -c 200
   curl -s "https://api.lever.co/v0/postings/SLUG?mode=json" | head -c 200
   ```
   A Lever bad slug returns HTTP 200 with `{"ok": false}` -- check the body,
   not the status code.
4. Ask about `keywords` (what makes a posting worth a look -- only used to
   filter the keyword-scoped aggregators, himalayas/hn/remoteok, not the ATS
   boards), `exclude_keywords` (soft de-prioritize signal), and `preferences`
   (locations, remote requirement, salary floor, company stage, anything
   else). Leave fields blank/empty for "no preference" -- don't guess a value
   the user didn't give you. These are judgment context for the model via
   `get_profile`, never a hard filter in code (see AGENTS.md's design rule);
   don't add filtering logic anywhere else to compensate.

## 4. Register the MCP server

Check first: `claude mcp list` and look for `jobhunt`. If it's already there,
skip this step.

Otherwise, register it with the absolute path to the venv's Python:

```bash
claude mcp add jobhunt -- /absolute/path/to/.venv/Scripts/python.exe -m jobhunt.server
```

Tell the user they need to restart their Claude Code session for a newly
registered (or changed) MCP server to be picked up -- this skill cannot do
that part for them.

## 5. Verify end to end

Once the server is registered and the session restarted (or right away via
the CLI, which needs no restart):

```bash
jobhunt sync
jobhunt stats
```

A clean run with no `errors` in the sync report and a non-zero job count in
`stats` means the setup is good. If `errors` lists a board, walk back to step
3 and recheck that slug against the live endpoint.

## 6. Hand off

Once resume, targets, and the server are all in place, point the user at the
`triage-jobs` skill for the actual day-to-day workflow (sync + shortlist +
score). Don't run a full triage yourself as part of setup unless they ask --
this skill's job is just getting the tool into a working state.
