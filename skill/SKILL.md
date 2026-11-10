---
name: media-intake
description: "Use when staging Instagram saved folders or profiles into Media OCR intake."
source: user-created
risk: medium
---

# Media Intake

Use this workflow when media must be **acquired** before Media OCR can read it —
specifically Instagram saved posts, named saved collections, profiles, highlights,
or single posts. Media OCR does not download anything; this stage does, and then
hands Media OCR a directory that matches its own intake convention.

This is Mode 3 of Media OCR's Acquisition Contract ("automated collection"), kept
as a separate design so the OCR tool keeps its no-network boundary.

## Before any collection: the authorisation question

Collection is not authorised by feasibility. Confirm all of the following and
record the answers in the run's notes:

1. **Whose content?** Your own saved folder, or someone else's public posts.
   Private accounts and login-walled content require that account's session — a
   session you hold is not the same as permission to copy.
2. **What will it be used for?** Personal reference reading is a different act from
   republishing, dataset building, or training.
3. **What does the platform allow?** Instagram's terms constrain automated
   collection. State this plainly if the operator asks; do not paper over it.
4. **How much?** Prefer one named collection over "all saved posts". Collection
   without a question is hoarding, and it burns rate limit you may need later.

If the operator cannot answer (1) and (2), stop and ask. Do not run a folder-scale
fetch on an assumption.

## Procedure

### 1. Preflight

```bash
export GALLERY_DL_BINARY="$PWD/.venv/bin/gallery-dl"
python skill/scripts/intake_ig.py check \
  --cookies-from-browser brave --keyring gnomekeyring
```

`check` verifies the binary, its version, and whether auth material is present. It
exits non-zero when no cookies are supplied, because only public targets could then
work — and on Instagram, usually not even those.

**Chromium-family browsers on Linux need `--keyring gnomekeyring`.** Their cookie
values are v11-encrypted with a key held in the OS keyring, and gallery-dl's
desktop-environment auto-detection falls back to `basictext` when the shell has no
`XDG_CURRENT_DESKTOP`, finds no key, and reports "Extracted 0 cookies" while the
request then fails as a login redirect. The tool forces the desktop hint when a
keyring is named, and requires `secretstorage` in the environment.

### 2. Plan the target, always

```bash
python skill/scripts/intake_ig.py plan \
  --target "https://www.instagram.com/USER/saved/all-posts/" \
  --cookies-from-browser firefox
```

`plan` enumerates a sample without downloading media. Report to the operator:
how many posts, how many media files, how many videos, and the date range. If the
sample already shows hundreds of posts, confirm scale before fetching. `--all`
enumerates the entire target (metadata only) — use it when the operator asks "how
big is this", and caveat the runtime: each media file is one HTTP request, so about
1000 files ≈ 0.8-1.7 hours at the default `--sleep-request`.

`plan` returns non-zero when gallery-dl reported an error, **even if the exit code
was 0** — gallery-dl reports an auth redirect as `AbortExtraction` while exiting
successfully. Trust the message stream, not the exit status.

### 3. Fetch

```bash
python skill/scripts/intake_ig.py fetch \
  --target "https://www.instagram.com/USER/saved/RECIPES/123456789/" \
  --out ~/Documents/knowledge-vault/Inbox/media/_instagram \
  --cookies-from-browser firefox --all
```

- `--max-posts` defaults to 200; `--all` is required for an entire folder.
- `--images-only` skips videos and Reels; use it when the target is text-on-image
  and you do not want video OCR in scope.
- Downloads are recorded in `_archive/archive.sqlite3`, so a re-run resumes instead
  of re-fetching. Do not use `--no-archive` for anything you may need to resume.
- Media is hardlinked into `_inbox/`, not copied, where the filesystem allows it.

### 3b. Rate limits: the cross-run cooldown

Two layers, and they are not the same thing:

- **Inside a run**, gallery-dl retries a 429 `--retries` times (default 4) with
  `--sleep-429` (default 60-120s) between tries. It does not read `Retry-After`,
  so it cannot match the platform's stated limit; that layer buys roughly 4-8
  minutes of tolerance.
- **Across runs**, the tool persists a ladder at `<out>/cooldown.json` — 5m, 15m,
  30m, 2h, 6h, 12h for consecutive 429s. A 429 records the cooldown and exits
  non-zero with the resume time; a later `fetch` refuses to start while it is live;
  one clean run clears it and resets the ladder.

`--auto-retry N` waits out the cooldown and resumes for up to N extra attempts,
capped by `--max-wait` (default 3600s per wait). `status` reports cooldown state,
archive size, and inbox size at any time.

Never respond to a 429 by lowering `--sleep-request`, raising concurrency, or
rotating sessions. Wait, then resume — the archive makes resuming cheap.

### 4. Read the manifest before reporting success

`<out>/intake-manifest.json` carries:

- `counts.posts_in_archive` vs `counts.posts_staged_this_run` vs `counts.posts_skipped`;
- `items[]` — per post: shortcode, URL, creator, date, caption, counts, inbox dir;
- `skipped[]` — each with a reason, so nothing vanishes silently;
- `errors[]`/`hints[]` — actionable causes, including rate limiting and auth.

Report those numbers as they are. Never report "downloaded everything" when the
manifest says posts were skipped or the archive is smaller than the plan suggested.

### 5. Hand off to Media OCR

Per item directory, one at a time, so provenance relationships stay intact:

```bash
python ../media-ocr/skill/scripts/ocr_batch.py --input <out>/_inbox/<item-dir> --check
python ../media-ocr/skill/scripts/ocr_batch.py --input <out>/_inbox/<item-dir>
```

Video-only items are flagged in the manifest with `ocr_entry_point:
scripts/ocr_video.py`; do not send them to `ocr_batch.py`, which reads images only.

OCR output is a **draft**. The review pass in Media OCR's own skill still applies:
compare the draft against the media, correct proper nouns and numbers, then promote
to a final note. Staging media does not make it knowledge, and a caption is not a
verified quotation.

## Re-projecting without network

`project` rebuilds `_inbox/` from `_archive/` and needs neither gallery-dl nor a
session. Use it after changing naming, filtering, or the provenance template:

```bash
python skill/scripts/intake_ig.py project --out ~/Documents/.../_instagram
```

Existing inbox directories are skipped, keyed by `post_shortcode` in each
`.intake.json`, so re-projection never renumbers or duplicates staged media.

## Failure modes and what they mean

| Symptom | Cause | Response |
|---|---|---|
| `AbortExtraction` / "redirect to login page" | No usable session, or cookies extracted as 0 | Check the keyring: Chromium-family browsers need `--keyring gnomekeyring` and `secretstorage` installed |
| `429 Too Many Requests` | Rate limited | Run stops and records a cooldown. Wait it out; `--auto-retry N` to resume automatically |
| `Cannot import yt-dlp` | Missing video dependency | `uv pip install --python .venv/bin/python yt-dlp` |
| 0 posts, exit 0, no errors | Target empty, or session lacks access to that collection | Verify the collection id in a browser |
| Sidecars but no inbox dirs | Media not written, or unsupported suffix | Check `skipped[]` reasons in the manifest |
| `Checkpoint` / `consent` in stderr | Instagram wants interactive confirmation | Resolve in a browser session; automated tools cannot consent |

## Constraints

- Never invent provenance. Unavailable fields are omitted, not filled in. A handle
  or URL never establishes authorship, permission, or truth.
- Never bypass access controls, login walls, private-account restrictions, or rate
  limits. If Instagram blocks the session, that is the answer.
- Never store cookies in a project directory. Pass them from the browser session or
  a path outside the repo; they are credentials with full account scope.
- Never run folder-scale collection without stating the scale to the operator first.
