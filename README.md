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
them, so a session is mandatory:

```bash
# 1. Preflight
.venv/bin/python skill/scripts/intake_ig.py check --cookies-from-browser firefox

# 2. Look before you leap — no media downloaded
.venv/bin/python skill/scripts/intake_ig.py plan \
  --target "https://www.instagram.com/YOUR_USER/saved/all-posts/" \
  --cookies-from-browser firefox

# 3. Collect one whole saved folder
.venv/bin/python skill/scripts/intake_ig.py fetch \
  --target "https://www.instagram.com/YOUR_USER/saved/Recipes/17895695668004550/" \
  --out ~/Documents/knowledge-vault/Inbox/media/_instagram \
  --cookies-from-browser firefox --all

# 4. OCR it with Media OCR, one item directory at a time
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
