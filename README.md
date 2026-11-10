# Media Intake

The acquisition stage that [Media OCR](../media-ocr) deliberately excludes.

Media OCR's Acquisition Contract defines three modes and implements two, leaving
"automated collection" as *"a separate approved design"*. This project is that
design, kept in its own repository so the OCR tool stays downloader-free.

```
Instagram ──gallery-dl──> _archive/  (untouched record, resumable)
                             │
                             └──project──> _inbox/<date>_<creator>_<title>_<shortcode>/
                                              01.jpg  02.jpg  03.jpg  source.md
                                                    │
                                              Media OCR (ocr_batch.py)
```

## What it does

1. **`plan`** — enumerates a target without downloading media, so you can see the
   size and shape of a collection before committing to it.
2. **`fetch`** — downloads through `gallery-dl`, then projects the result into
   Media OCR's intake convention: one directory per post, zero-padded media names,
   and a `source.md` carrying only provenance Instagram actually delivered.
3. **`project`** — re-projects the inbox from an existing archive with no network
   access at all, so you can re-derive intake after changing naming or filtering.

Media files are hardlinked into the inbox where the filesystem allows it, so a
typical run costs no extra disk.

## Install

```bash
uv venv .venv --python 3.14
uv pip install --python .venv/bin/python -r docs/requirements.txt
export GALLERY_DL_BINARY="$PWD/.venv/bin/gallery-dl"   # or put gallery-dl on PATH
```

## Use

Saved posts and named collections are visible only to the account that saved
them, so a session is mandatory. On Linux, Chromium-family browsers (Brave,
Chrome, Chromium) encrypt cookie values with an OS keyring key — pass
`--keyring gnomekeyring` or gallery-dl silently extracts **zero** cookies and
every request looks like a login wall:

```bash
# 1. Preflight
.venv/bin/python skill/scripts/intake_ig.py check \
  --cookies-from-browser brave --keyring gnomekeyring

# 2. Look before you leap — no media downloaded
.venv/bin/python skill/scripts/intake_ig.py plan \
  --target "https://www.instagram.com/YOUR_USER/saved/all-posts/" \
  --cookies-from-browser brave --keyring gnomekeyring

# 2b. ...or count the whole pile first (metadata only, slower)
.venv/bin/python skill/scripts/intake_ig.py plan \
  --target "https://www.instagram.com/YOUR_USER/saved/all-posts/" \
  --cookies-from-browser brave --keyring gnomekeyring --all

# 3. Collect one whole saved folder
.venv/bin/python skill/scripts/intake_ig.py fetch \
  --target "https://www.instagram.com/YOUR_USER/saved/Recipes/17895695668004550/" \
  --out ~/Documents/knowledge-vault/Inbox/media/_instagram \
  --cookies-from-browser brave --keyring gnomekeyring --all --auto-retry 3

# 4. Check cooldown / progress
.venv/bin/python skill/scripts/intake_ig.py status \
  --out ~/Documents/knowledge-vault/Inbox/media/_instagram

# 5. OCR it with Media OCR, one item directory at a time
.venv/bin/python ../media-ocr/skill/scripts/ocr_batch.py \
  --input ~/Documents/knowledge-vault/Inbox/media/_instagram/_inbox/<item-dir>
```

Supported targets: `/<user>/saved/`, `/<user>/saved/all-posts/`,
`/<user>/saved/<collection>/<id>`, `/<user>/`, `/<user>/reels/`,
`/<user>/highlights/`, `/<user>/stories/`, `/p/<shortcode>/`,
`/explore/tags/<tag>/`.

`--max-posts` defaults to **200**. A whole saved folder needs `--all`, stated
explicitly, because that is the difference between a sample and an archive.

## Output layout

```text
<out>/
  gallery-dl.json        # the exact config used, rate limits included
  intake-manifest.json   # latest run: counts, items, skips, errors, hints
  runs.jsonl             # one summary line per run, append-only
  _archive/              # gallery-dl's own layout + .json sidecars (never edited)
    archive.sqlite3      # download archive → resume without re-fetching
  _inbox/                # what Media OCR reads
    2026-02-01_creator_title-slug_AAA111/
      01.jpg 02.jpg source.md .intake.json
```

`intake-manifest.json` distinguishes posts *in the archive* from posts *staged this
run* from posts *skipped*. That is deliberate: "downloaded 131" is not the same
claim as "the collection had 143 items".

## Whole-collection runs

`run_full.py` chains the phases and is resumable at each of them:

```bash
python skill/scripts/run_full.py --out DIR --target URL --phase all \
  --cookies-from-browser brave --keyring gnomekeyring
```

| Phase | Does |
|---|---|
| `fetch` | loops through cooldowns until the target is exhausted |
| `ocr` | image OCR over item dirs in parallel, skipping existing drafts |
| `video` | text-on-video OCR via media-ocr's `ocr_video_batch.py` |
| `all` | fetch, then ocr, then video |

Each clip becomes a sibling of its own media (`<item>/01/`), so an item directory
stays self-contained. Videos are deferred by default because sampling every frame
of a thousand Reels is hours of CPU; run `--phase video` when you want them.
`RUN-REPORT.md` is rewritten at the end with real counts, failures, and caveats.

## Rate limits: the retry timer

There are two layers, because they answer different questions.

**Inside one run** gallery-dl retries a 429 up to `--retries` (default 4) times,
waiting `--sleep-429` (default 60-120s) between tries. It does **not** read the
platform's `Retry-After` header — Instagram does not publish a usable one on this
path — so that layer is a fixed schedule, not a matched one. Roughly 4-8 minutes
of tolerance before it gives up.

**Across runs** this tool keeps a cooldown at `<out>/cooldown.json`, because
gallery-dl's in-run retries cannot see that you were blocked ten minutes ago:

| Consecutive 429s | Cooldown |
|---|---|
| 1 | 5 min |
| 2 | 15 min |
| 3 | 30 min |
| 4 | 2 h |
| 5 | 6 h |
| 6+ | 12 h |

- A 429 records (or escalates) the cooldown and the run exits non-zero with the
  resume time. Nothing silently pretends to have finished.
- The next `fetch` refuses to start while a cooldown is live, and tells you how
  long is left. `status` shows it at any time.
- `--auto-retry N` waits out the cooldown and resumes, up to N extra attempts.
  `--max-wait` (default 3600s) caps any single wait, so it will not sit for six
  hours inside one command.
- One clean run clears the cooldown and resets the ladder, appending the cleared
  record to `rate-limit-history.jsonl`.
- Resuming is cheap and safe: the download archive means already-fetched media is
  never requested twice.

Why a ladder rather than "wait exactly what the server says": matching a limit you
cannot read is guesswork, and repeated retries against a throttle are what turn a
temporary block into a longer one. The ladder is deliberately conservative and
escalates only when you keep being refused.

### Setting expectations on scale

`--sleep-request 3.0-6.0` applies per HTTP request, and each media file is one
request. As a rule of thumb: **1000 files ≈ 0.8-1.7 hours**, so a large saved
collection is an overnight job, not a coffee break. Run `plan --all` first to get
the real count, then decide whether to take the whole thing or one named
collection. `--images-only` roughly halves it by skipping video.

## Limits you should expect

- **Rate limiting is real.** Instagram throttles folder-scale reads. Defaults are
  `--sleep-request 3.0-6.0` and `--sleep-429 60-120`; a 200-post collection takes
  tens of minutes by design. Aggressive settings get accounts temporarily blocked,
  and the block is reported by the platform, not by this tool.
- **Saved collections need collections to exist.** The named-collection URL
  requires the numeric collection id, which appears in the URL when you open a
  collection in a browser.
- **Nothing here grants rights.** Metadata can be missing, wrong, or stale.
  `source.md` records what was *observed*, and Media OCR's review layer is where
  attribution gets decided. See `SECURITY.md` for the operator responsibilities.
- **Anonymous access mostly fails.** Instagram redirects unauthenticated requests
  to a login page. `gallery-dl` reports that as `AbortExtraction` in its JSON
  stream while exiting `0` — this tool checks the message stream, not just the
  exit code, so you get the failure instead of an empty inbox.

## Test

```bash
.venv/bin/python -m unittest discover -s skill/scripts -p 'test_*.py' -v
```

The suite includes contract tests that load Media OCR's own `parse_source()` and
`image_paths()` from `../media-ocr/skill/scripts/ocr_batch.py` and assert that
generated `source.md` files parse and staged images are discoverable. If Media
OCR's intake convention changes, these tests fail here first.

## Project documents

- `skill/SKILL.md` — agent-facing operating procedure and review gates
- `SECURITY.md` — authorisation, rate-limit, and data-handling responsibilities
- `docs/requirements.txt` — pinned runtime
- `LICENSE` — Apache License 2.0
- `NOTICE` — copyright and attribution notice
