#!/usr/bin/env python3
"""Stage Instagram media into the Media OCR intake layout.

This is the separate acquisition stage that Media OCR deliberately does not
include. It delegates all network work to an installed ``gallery-dl`` binary,
keeps gallery-dl's own archive as the untouched record, and *projects* a
media-ocr-compatible inbox (one directory per post, ``01.jpg``, ``02.jpg``,
``source.md``) from that archive.

Nothing here invents provenance. Every field written to ``source.md`` comes
from metadata Instagram delivered, or is left out.

Subcommands
-----------
check    Preflight: gallery-dl present, version, cookie/auth material visible.
plan     Enumerate what a target would yield, without downloading media.
fetch    Download into the archive, then project into the inbox.
project  Re-project the inbox from an existing archive. No network.

Requires: gallery-dl on PATH or via --gallery-dl.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

TOOL = "media-intake"
VERSION = "0.1.0"

IMAGE_SUFFIXES = {".avif", ".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

# Media OCR's own limit; keep captions on one line so its source.md parser
# (`^- \*\*(.+?):\*\*\s*(.*)$`) keeps matching.
CAPTION_LIMIT = 300

TARGET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("collection", re.compile(r"^https?://(?:www\.)?instagram\.com/(?!(?:p|tv|reel|explore|stories)/)(?P<user>[^/?#]+)/saved/(?P<collection>[^/?#]+)/(?P<collection_id>[^/?#]+)")),
    ("saved", re.compile(r"^https?://(?:www\.)?instagram\.com/(?!(?:p|tv|reel|explore|stories)/)(?P<user>[^/?#]+)/saved(?:/all-posts)?/?$")),
    ("highlights", re.compile(r"^https?://(?:www\.)?instagram\.com/(?P<user>[^/?#]+)/(?:highlights|covers)(?:/covers)?/?$")),
    ("stories", re.compile(r"^https?://(?:www\.)?instagram\.com/stories/(?P<user>[^/?#]+)")),
    ("post", re.compile(r"^https?://(?:www\.)?instagram\.com/(?:[^/?#]+/)?(?:p|tv|reels?)/(?P<shortcode>[^/?#]+)")),
    ("tag", re.compile(r"^https?://(?:www\.)?instagram\.com/explore/tags/(?P<tag>[^/?#]+)")),
    ("profile", re.compile(r"^https?://(?:www\.)?instagram\.com/(?!(?:p|tv|reel|explore|stories)/)(?P<user>[^/?#]+)/?(?P<section>posts|reels|tagged)?/?$")),
]

# Anything at folder scale needs a logged-in session. Named collections and
# saved posts are private to the account that saved them.
AUTH_REQUIRED_MODES = {"saved", "collection", "stories", "highlights"}

# Escalating cooldown after a 429. Instagram publishes no usable Retry-After
# for this path, so the schedule has to be ours; it persists across runs so
# repeated attempts back off instead of hammering.
COOLDOWN_LADDER_SECONDS = (300, 900, 1800, 7200, 21600, 43200)
RATE_LIMIT_MARKERS = ("429", "too many requests", "please wait a few minutes", "rate limit")


class IntakeError(RuntimeError):
    """A condition the operator must resolve before collection continues."""


# --------------------------------------------------------------------------
# classification and formatting helpers
# --------------------------------------------------------------------------


def classify_target(url: str) -> dict[str, str]:
    """Return {mode, user/tag/shortcode...} for a supported Instagram URL."""
    cleaned = url.strip()
    for mode, pattern in TARGET_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            groups = {key: value for key, value in match.groupdict().items() if value}
            return {"mode": mode, **groups}
    raise IntakeError(
        f"Unsupported target URL: {cleaned}\n"
        "Supported: /<user>/saved/all-posts/, /<user>/saved/<collection>/<id>, "
        "/<user>/, /<user>/reels/, /p/<shortcode>/, /explore/tags/<tag>/"
    )


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def single_line(value: Any, limit: int = CAPTION_LIMIT) -> str:
    """Collapse untrusted text to one bounded line."""
    text = " ".join(str(value).split())
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def slugify(value: Any, limit: int = 40) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    if limit and len(text) > limit:
        text = text[:limit].rstrip("-")
    return text or "item"


def date_only(value: Any) -> str:
    """gallery-dl dates arrive as 'YYYY-MM-DD HH:MM:SS'; keep the day."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
    return match.group(1) if match else ""


def media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return "other"


def md_value(value: Any, limit: int = CAPTION_LIMIT) -> str:
    """Keep a value on one line and bounded so Media OCR's bullet parser matches."""
    return single_line(value, limit=limit).replace("\r", " ").replace("\n", " ")


# --------------------------------------------------------------------------
# rate-limit cooldown
# --------------------------------------------------------------------------


def is_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def cooldown_path(out: Path) -> Path:
    return out / "cooldown.json"


def read_cooldown(out: Path) -> dict[str, Any] | None:
    path = cooldown_path(out)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "until" not in data:
        return None
    return data


def cooldown_remaining(cooldown: dict[str, Any] | None, *, now: datetime | None = None) -> float:
    """Seconds left on the cooldown; <= 0 means expired."""
    if not cooldown:
        return 0.0
    try:
        until = datetime.fromisoformat(str(cooldown["until"]))
    except (KeyError, ValueError):
        return 0.0
    reference = now or datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return (until - reference).total_seconds()


def record_rate_limit(out: Path, *, reason: str, seconds: int | None = None) -> dict[str, Any]:
    """Persist an escalating cooldown. Returns the new state."""
    previous = read_cooldown(out)
    level = int(previous.get("level", 0)) if previous and cooldown_remaining(previous) > 0 else int((previous or {}).get("level", 0))
    level = min(level + 1, len(COOLDOWN_LADDER_SECONDS))
    wait = int(seconds if seconds is not None else COOLDOWN_LADDER_SECONDS[level - 1])
    until = datetime.now(timezone.utc) + timedelta(seconds=wait)
    state = {
        "until": until.replace(microsecond=0).isoformat(),
        "seconds": wait,
        "level": level,
        "reason": reason,
        "recorded_at": iso_now(),
    }
    out.mkdir(parents=True, exist_ok=True)
    cooldown_path(out).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def clear_rate_limit(out: Path) -> bool:
    """A clean run resets the ladder. Returns True when something was cleared."""
    state = read_cooldown(out)
    path = cooldown_path(out)
    if not path.is_file():
        return False
    path.unlink()
    if state:
        history = out / "rate-limit-history.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**state, "cleared_at": iso_now()}, ensure_ascii=False) + "\n")
    return True


def explain_cooldown(state: dict[str, Any], remaining: float) -> str:
    minutes = max(1, round(remaining / 60))
    return (
        f"Rate-limit cooldown active: level {state.get('level')}, "
        f"{minutes} minute(s) left (until {state.get('until')}).\n"
        f"Reason: {state.get('reason')}\n"
        "Re-run after that, or pass --auto-retry N to wait and resume automatically. "
        "Downloads already in the archive are not repeated."
    )


# --------------------------------------------------------------------------
# gallery-dl invocation
# --------------------------------------------------------------------------


@dataclass
class GalleryDL:
    binary: str = "gallery-dl"
    version: str | None = None

    def resolve(self) -> str:
        binary = self.binary
        if os.sep in binary:
            if not Path(binary).is_file():
                raise IntakeError(f"gallery-dl not found at {binary}")
            return binary
        found = shutil.which(binary)
        if not found:
            raise IntakeError(
                "gallery-dl is not installed.\n"
                "Install it in this project's environment:\n"
                "  uv pip install --python .venv/bin/python gallery-dl"
            )
        return found

    def probe_version(self) -> str:
        result = subprocess.run([self.resolve(), "--version"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise IntakeError(f"gallery-dl --version failed: {result.stderr.strip()}")
        self.version = result.stdout.strip() or "unknown"
        return self.version

    def run(self, args: Sequence[str], timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = None
        if env:
            merged = {**os.environ, **env}
        return subprocess.run([self.resolve(), *args], capture_output=True, text=True, timeout=timeout, env=merged)


def browser_spec(browser: str | None, keyring: str | None) -> str | None:
    """gallery-dl expects BROWSER[/DOMAIN][+KEYRING][:PROFILE]."""
    if not browser:
        return None
    if keyring and "+" not in browser:
        return f"{browser}+{keyring}"
    return browser


def keyring_env(spec: str | None) -> dict[str, str] | None:
    """Chromium keyring auto-detection fails in agent shells with no desktop env.

    gallery-dl picks 'basictext' when XDG_CURRENT_DESKTOP is unset, finds no key,
    and silently extracts zero cookies. Force the desktop hint when we know the
    backend, so a fresh shell behaves like the operator's own terminal.
    """
    if not spec or "gnomekeyring" not in spec:
        return None
    if os.environ.get("XDG_CURRENT_DESKTOP"):
        return None
    return {"XDG_CURRENT_DESKTOP": "GNOME"}


def parse_browser_spec(spec: str) -> list[str | None]:
    """Decompose BROWSER[/DOMAIN][+KEYRING][:PROFILE] the way gallery-dl's CLI does.

    gallery-dl stores this as the ``cookies`` config value (a 5-element list);
    a bare ``cookies-from-browser`` config key is silently ignored.
    """
    browser, _, profile = spec.partition(":")
    browser, _, keyring = browser.partition("+")
    browser, _, domain = browser.partition("/")
    if profile and profile[0] == ":":
        profile, container = None, profile[1:]
    else:
        profile, _, container = profile.partition("::")
    return [browser or None, profile or None, keyring or None, container or None, domain or None]


def build_config(
    *,
    cookies_file: Path | None = None,
    cookies_from_browser: str | None = None,
    archive: Path | None = None,
    sleep_request: str = "3.0-6.0",
    sleep_429: str = "60-120",
    retries: int = 4,
    directory_format: Sequence[str] | None = None,
    filename_format: str = "{num:02d}_{shortcode}.{extension}",
    videos: bool = True,
) -> dict[str, Any]:
    """Build the gallery-dl config explicitly, so the rate limits are visible."""
    extractor: dict[str, Any] = {
        "instagram": {
            "directory": list(directory_format or ["instagram", "{username}", "{post_date:%Y-%m-%d}_{post_shortcode}"]),
            "filename": filename_format,
            "videos": videos,
            "sleep-request": sleep_request,
            "sleep-429": sleep_429,
            "retries": retries,
        }
    }
    if cookies_file:
        extractor["instagram"]["cookies"] = str(cookies_file)
    elif cookies_from_browser:
        extractor["instagram"]["cookies"] = parse_browser_spec(cookies_from_browser)

    config: dict[str, Any] = {"extractor": extractor, "output": {"mode": "auto"}}
    if archive:
        config["downloader"] = {"archive": str(archive)}
    return config


def write_config(config_path: Path, config: dict[str, Any]) -> Path:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def parse_json_lines(stdout: str) -> list[list[Any]]:
    """Parse gallery-dl -j output.

    Current gallery-dl pretty-prints one JSON array of message arrays
    (``[[-1, {...}], [3, "url", {...}]]``) unless ``output.jsonl`` is set, so
    accept both the whole document and one-array-per-line.
    """
    text = stdout.strip()
    if not text:
        return []
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, list) and all(isinstance(row, list) for row in document):
        return [row for row in document]

    rows: list[list[Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            rows.append(value)
    return rows


def collect_errors(rows: Iterable[list[Any]]) -> list[dict[str, str]]:
    """Pull gallery-dl's own error messages out of the message stream.

    gallery-dl exits 0 even when extraction aborted (a login redirect is
    reported as ``AbortExtraction``), so exit status alone is not evidence.
    """
    errors: list[dict[str, str]] = []
    for row in rows:
        if len(row) < 2 or row[0] != -1:
            continue
        payload = row[1]
        if isinstance(payload, dict):
            errors.append(
                {
                    "error": str(payload.get("error") or "unknown"),
                    "message": str(payload.get("message") or ""),
                }
            )
        elif isinstance(payload, str):
            errors.append({"error": "unknown", "message": payload})
    return errors


def aggregate_posts(rows: Iterable[list[Any]]) -> list[dict[str, Any]]:
    """Collapse per-file Url messages into one record per post."""
    posts: dict[str, dict[str, Any]] = {}
    for row in rows:
        if len(row) < 3 or row[0] != 3 or not isinstance(row[2], dict):
            continue
        meta = row[2]
        shortcode = meta.get("post_shortcode") or meta.get("shortcode") or ""
        if not shortcode:
            continue
        post = posts.setdefault(
            shortcode,
            {
                "post_shortcode": shortcode,
                "post_url": meta.get("post_url"),
                "username": meta.get("username"),
                "post_date": date_only(meta.get("post_date") or meta.get("date")),
                "type": meta.get("type"),
                "caption": md_value(meta.get("description") or ""),
                "collection": meta.get("collection_name"),
                "media": [],
            },
        )
        post["media"].append(
            {
                "num": meta.get("num"),
                "kind": "video" if meta.get("video_url") else media_kind(Path(str(meta.get("filename") or meta.get("extension") or ""))),
                "extension": meta.get("extension"),
            }
        )
    for post in posts.values():
        post["media"].sort(key=lambda item: (item["num"] is None, item["num"]))
        post["media_count"] = len(post["media"])
        post["kind"] = "video" if all(media["kind"] == "video" for media in post["media"]) else "image"
    return sorted(posts.values(), key=lambda post: (post["post_date"], post["post_shortcode"]))


def diagnose_failure(stderr: str, returncode: int, errors: Sequence[dict[str, str]] | None = None) -> list[str]:
    """Turn gallery-dl noise into instructions the operator can act on."""
    hints: list[str] = []
    joined = " ".join([stderr, *(f"{item.get('error', '')} {item.get('message', '')}" for item in (errors or []))])
    lowered = joined.lower()
    if any(token in lowered for token in ("login", "unauthori", "401", "403", "checkpoint", "consent", "abortextraction")):
        hints.append(
            "Instagram refused the request without a usable session. Export cookies from a "
            "logged-in browser and pass --cookies-from-browser <firefox|chrome> or --cookies <file.txt>."
        )
    if "429" in joined or "please wait a few minutes" in lowered:
        hints.append(
            "Instagram rate-limited this session. Stop, wait (minutes to hours), lower --max-posts, "
            "and raise --sleep-request before retrying."
        )
    if returncode == 64:
        hints.append("gallery-dl does not support that URL. Check the target form with `plan`.")
    if not hints and (returncode != 0 or errors):
        hints.append("gallery-dl reported a problem; read the stderr tail above before retrying.")
    return hints


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def command_plan(args: argparse.Namespace, gdl: GalleryDL) -> int:
    target = classify_target(args.target)
    gdl.probe_version()

    archive_dir = Path(args.out).expanduser().resolve() / "_archive"
    spec = browser_spec(args.cookies_from_browser, args.keyring)
    config = build_config(
        cookies_file=Path(args.cookies).expanduser() if args.cookies else None,
        cookies_from_browser=spec,
        sleep_request=args.sleep_request,
        sleep_429=args.sleep_429,
    )
    config_path = write_config(Path(args.out).expanduser().resolve() / "gallery-dl.json", config)

    sample = args.max_posts
    argv = ["--config-json", str(config_path), "-j"]
    if sample:
        argv += ["--range", f"1-{sample}"]
    argv.append(args.target)
    scope = f"sampling up to {sample} media file(s)" if sample else "enumerating the whole target"
    print(f"# plan: {target['mode']} → {scope}, no download", file=sys.stderr)
    result = gdl.run(argv, timeout=args.timeout, env=keyring_env(spec))

    rows = parse_json_lines(result.stdout)
    errors = collect_errors(rows)
    posts = aggregate_posts(rows)
    payload = {
        "tool": TOOL,
        "version": VERSION,
        "gallery_dl": gdl.version,
        "target": args.target,
        "target_mode": target["mode"],
        "sampled_media_files": sample,
        "auth_required": target["mode"] in AUTH_REQUIRED_MODES,
        "auth_supplied": bool(args.cookies or args.cookies_from_browser),
        "archive_dir": str(archive_dir),
        "returncode": result.returncode,
        "posts_in_sample": len(posts),
        "media_in_sample": sum(post["media_count"] for post in posts),
        "videos_in_sample": sum(1 for post in posts for media in post["media"] if media["kind"] == "video"),
        "posts": posts,
        "errors": errors,
        "hints": diagnose_failure(result.stderr, result.returncode, errors),
        "stderr_tail": [line for line in result.stderr.strip().splitlines()[-6:]],
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"target      : {args.target} ({target['mode']})")
        print(f"gallery-dl  : {gdl.version}")
        print(f"auth needed : {payload['auth_required']}  supplied: {payload['auth_supplied']}")
        print(f"sample      : {payload['posts_in_sample']} post(s), {payload['media_in_sample']} media file(s), {payload['videos_in_sample']} video(s)")
        for post in posts[:10]:
            print(f"  - {post['post_date'] or '????-??-??'}  @{post['username']}  {post['post_shortcode']}  {post['media_count']} file(s)  {post['type']}")
        if len(posts) > 10:
            print(f"  … {len(posts) - 10} more in sample")
        for error in errors:
            print(f"x {error['error']}: {error['message']}")
        for hint in payload["hints"]:
            print(f"! {hint}")
    return 0 if posts and not errors else 1


# --------------------------------------------------------------------------
# projection: archive → Media OCR inbox
# --------------------------------------------------------------------------


@dataclass
class StagedItem:
    post_shortcode: str
    post_url: str | None
    username: str | None
    fullname: str | None
    post_date: str
    caption: str
    collection: str | None
    location: str | None
    tags: list[str]
    kind: str
    source_dir: Path
    media: list[Path] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def read_sidecars(archive_root: Path) -> list[StagedItem]:
    """Group gallery-dl --write-metadata sidecars into one item per post folder."""
    items: list[StagedItem] = []
    seen: set[Path] = set()
    for sidecar in sorted(archive_root.rglob("*.json")):
        parent = sidecar.parent
        if parent in seen:
            continue
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict) or not meta.get("post_shortcode"):
            continue
        seen.add(parent)
        media = sorted(
            (path for path in parent.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES),
            key=lambda path: (path.name.split("_")[0].zfill(4), path.name.lower()),
        )
        items.append(
            StagedItem(
                post_shortcode=str(meta["post_shortcode"]),
                post_url=meta.get("post_url"),
                username=meta.get("username"),
                fullname=meta.get("fullname"),
                post_date=date_only(meta.get("post_date") or meta.get("date")),
                caption=md_value(meta.get("description") or ""),
                collection=meta.get("collection_name"),
                location=meta.get("location_name"),
                tags=list(meta.get("tags") or []),
                kind="video" if meta.get("type") == "reel" else "image",
                source_dir=parent,
                media=media,
                meta=meta,
            )
        )
    return sorted(items, key=lambda item: (item.post_date, item.post_shortcode))


def inbox_dir_name(item: StagedItem) -> str:
    """Stable, human-scannable, collision-free: date_creator_slug_SHORTCODE."""
    date = item.post_date or "undated"
    creator = slugify(item.username or "unknown", 30)
    title = slugify(item.caption or "post", 40)
    return f"{date}_{creator}_{title}_{item.post_shortcode}"[:120]


def source_markdown(item: StagedItem, *, mode_label: str, acquisition: str, archive_rel: str, media_summary: str) -> str:
    lines = ["# Source", ""]
    lines.append(f"- **Local item ID:** {inbox_dir_name(item)}")
    lines.append(f"- **Acquisition mode:** {acquisition}")
    if item.post_url:
        lines.append(f"- **Source URL:** {item.post_url}")
    creator = f"@{item.username}" if item.username else ""
    if creator and item.fullname:
        creator = f"{creator} ({single_line(item.fullname, 80)})"
    if creator:
        lines.append(f"- **Creator/publisher:** {creator}")
        lines.append(f"- **Creator handle observed on slides:** @{item.username}")
    lines.append(f"- **Captured/imported:** {date_only(iso_now())}")
    if item.post_date:
        lines.append(f"- **Visible date:** {item.post_date}")
    if item.caption:
        lines.append(f"- **Caption (as delivered):** {item.caption}")
    if item.collection:
        lines.append(f"- **Collection:** {single_line(item.collection, 120)}")
    if item.location:
        lines.append(f"- **Location (as tagged):** {single_line(item.location, 120)}")
    if item.tags:
        lines.append(f"- **Tags (as tagged):** {single_line(' '.join(item.tags), 160)}")
    lines.append(f"- **Media staged:** {media_summary}")
    lines.append(f"- **Archive record:** {archive_rel}")
    lines.append(
        "- **Rights/attribution uncertainty:** Handle, caption and dates are as delivered by "
        "Instagram at capture time. Permission, authorship, and reuse rights are not verified. "
        "OCR text is a draft, not a quotation."
    )
    lines.append("")
    return "\n".join(lines)


def project(
    items: Sequence[StagedItem],
    inbox: Path,
    *,
    mode_label: str,
    acquisition: str,
    images_only: bool = False,
    existing: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Materialise inbox directories. Returns (records, skipped, warnings)."""
    inbox.mkdir(parents=True, exist_ok=True)
    existing = existing if existing is not None else scan_inbox(inbox)
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    for item in items:
        media = [path for path in item.media if media_kind(path) == "image"] if images_only else list(item.media)
        if not media:
            skipped.append({"post_shortcode": item.post_shortcode, "why": "no image files staged" if images_only else "no media files staged"})
            continue
        if item.post_shortcode in existing:
            skipped.append({"post_shortcode": item.post_shortcode, "why": "already in inbox", "inbox_dir": existing[item.post_shortcode]})
            continue

        target = inbox / inbox_dir_name(item)
        target.mkdir(parents=True, exist_ok=True)
        staged: list[dict[str, Any]] = []
        images = videos = others = 0
        for index, source in enumerate(media, start=1):
            destination = target / f"{index:02d}{source.suffix.lower()}"
            if not destination.exists():
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)
            kind = media_kind(destination)
            images += kind == "image"
            videos += kind == "video"
            others += kind == "other"
            staged.append({"file": destination.name, "kind": kind, "archive_file": str(source.relative_to(source.parent.parent))})

        summary = ", ".join(part for part in (f"{images} image(s)" if images else "", f"{videos} video(s)" if videos else "", f"{others} other" if others else "") if part) or "nothing"
        archive_rel = str(item.source_dir.name)
        (target / "source.md").write_text(
            source_markdown(item, mode_label=mode_label, acquisition=acquisition, archive_rel=archive_rel, media_summary=summary),
            encoding="utf-8",
        )
        (target / ".intake.json").write_text(
            json.dumps(
                {
                    "post_shortcode": item.post_shortcode,
                    "post_url": item.post_url,
                    "kind": "video" if videos and not images else "image",
                    "files": staged,
                    "ocr_entry_point": "scripts/ocr_video.py" if videos and not images else "scripts/ocr_batch.py",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if videos and not images:
            warnings.append(f"{target.name}: video-only item — OCR it with media-ocr's ocr_video.py, not ocr_batch.py")
        records.append(
            {
                "post_shortcode": item.post_shortcode,
                "post_url": item.post_url,
                "username": item.username,
                "post_date": item.post_date,
                "caption": item.caption,
                "caption_full": single_line(item.meta.get("description") or "", 0),
                "collection": item.collection,
                "kind": "video" if videos and not images else "image",
                "images": images,
                "videos": videos,
                "inbox_dir": target.name,
                "files": staged,
            }
        )
    return records, skipped, warnings


def scan_inbox(inbox: Path) -> dict[str, str]:
    """Map post_shortcode → existing inbox dir name, for idempotent re-runs."""
    found: dict[str, str] = {}
    if not inbox.is_dir():
        return found
    for record in inbox.glob("*/.intake.json"):
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        shortcode = data.get("post_shortcode")
        if shortcode:
            found[str(shortcode)] = record.parent.name
    return found


# --------------------------------------------------------------------------
# fetch / project commands
# --------------------------------------------------------------------------


def command_fetch(args: argparse.Namespace, gdl: GalleryDL) -> int:
    target = classify_target(args.target)
    version = gdl.probe_version()
    out = Path(args.out).expanduser().resolve()
    archive_dir = out / "_archive"
    inbox = out / "_inbox"
    archive_dir.mkdir(parents=True, exist_ok=True)

    cookies_file = Path(args.cookies).expanduser() if args.cookies else None
    if cookies_file and not cookies_file.is_file():
        raise IntakeError(f"Cookie file not found: {cookies_file}")
    spec = browser_spec(args.cookies_from_browser, args.keyring)
    if target["mode"] in AUTH_REQUIRED_MODES and not (cookies_file or spec):
        raise IntakeError(
            f"'{target['mode']}' targets are only visible to a logged-in account.\n"
            "Pass --cookies-from-browser <firefox|chrome|chromium|brave> (uses that browser's "
            "existing Instagram session) or --cookies <cookies.txt>.\n"
            "Chromium-family browsers on Linux also need --keyring gnomekeyring.\n"
            "Plan first to confirm the session works."
        )

    config = build_config(
        cookies_file=cookies_file,
        cookies_from_browser=spec,
        archive=None if args.no_archive else archive_dir / "archive.sqlite3",
        sleep_request=args.sleep_request,
        sleep_429=args.sleep_429,
        retries=args.retries,
        videos=not args.images_only,
    )
    config_path = write_config(out / "gallery-dl.json", config)

    argv = ["--config-json", str(config_path), "--write-metadata", "-D", str(archive_dir)]
    if args.max_posts:
        argv += ["--range", f"1-{args.max_posts}"]
    if args.images_only:
        argv += ["--filter", "extension not in ('mp4', 'mov', 'm4v', 'webm')"]
    argv.append(args.target)

    env = keyring_env(spec)
    attempts = 1 + max(0, args.auto_retry)
    attempt = 1
    while True:
        # Honour an outstanding cooldown before touching the network again.
        state = read_cooldown(out)
        remaining = cooldown_remaining(state)
        if remaining > 0 and state and not args.force:
            if attempt >= attempts:
                raise IntakeError(explain_cooldown(state, remaining))
            if remaining > args.max_wait:
                raise IntakeError(
                    f"{explain_cooldown(state, remaining)}\n"
                    f"Refusing to wait longer than --max-wait {args.max_wait}s in one go. "
                    "Re-run later, or raise --max-wait."
                )
            print(f"# rate limit: waiting {int(remaining)}s before attempt {attempt + 1} (level {state.get('level')})", file=sys.stderr)
            time.sleep(remaining)

        started = iso_now()
        print(f"# fetch attempt {attempt}/{attempts}: {target['mode']} → {archive_dir}", file=sys.stderr)
        print(f"# config: {config_path}", file=sys.stderr)
        result = gdl.run(argv, timeout=args.timeout, env=env)

        limited = is_rate_limited("\n".join((result.stderr, result.stdout[-4000:])))
        items = read_sidecars(archive_dir)
        records, skipped, warnings = project(
            items,
            inbox,
            mode_label=target["mode"],
            acquisition=f"Automated collection ({TOOL} {VERSION}; gallery-dl {version}; Instagram {target['mode']})",
            images_only=False,
        )
        hints = diagnose_failure(result.stderr, result.returncode)
        totals = {
            "posts_in_archive": len(items),
            "posts_staged_this_run": len(records),
            "posts_skipped": len(skipped),
            "images": sum(record["images"] for record in records),
            "videos": sum(record["videos"] for record in records),
        }
        cooldown_state = None
        if limited:
            cooldown_state = record_rate_limit(out, reason=f"Instagram 429 during attempt {attempt} for {args.target}")
            hints.append(f"Rate limit recorded. Cooldown level {cooldown_state['level']}: resume after {cooldown_state['until']} ({cooldown_state['seconds']}s).")
        elif records or totals["posts_in_archive"]:
            clear_rate_limit(out)

        manifest = {
            "tool": TOOL,
            "version": VERSION,
            "run_id": args.run_id or run_id(),
            "gallery_dl": version,
            "target": args.target,
            "target_mode": target["mode"],
            "started_at_utc": started,
            "finished_at_utc": iso_now(),
            "auth": {"method": "cookies-from-browser" if spec else ("cookies-file" if cookies_file else "none"), "spec": spec},
            "command": argv,
            "config_file": str(config_path),
            "archive_dir": str(archive_dir),
            "inbox_dir": str(inbox),
            "attempt": attempt,
            "gallery_dl_returncode": result.returncode,
            "rate_limited": limited,
            "cooldown": cooldown_state,
            "counts": totals,
            "items": records,
            "skipped": skipped,
            "warnings": warnings,
            "hints": hints,
            "stderr_tail": [line for line in result.stderr.strip().splitlines()[-12:]],
        }
        write_manifest(out, manifest)

        if limited and attempt < attempts:
            nxt = read_cooldown(out)
            wait = cooldown_remaining(nxt)
            if wait <= 0 or wait > args.max_wait:
                print_summary(manifest, json_output=args.json)
                raise IntakeError(
                    f"Rate limited again on attempt {attempt}. "
                    f"{'Cooldown expired.' if wait <= 0 else f'Next wait of {int(wait)}s exceeds --max-wait {args.max_wait}.'}\n"
                    "Stop here and retry later; repeated attempts deepen the block."
                )
            print(f"# rate limit on attempt {attempt}; waiting {int(wait)}s", file=sys.stderr)
            time.sleep(wait)
            attempt += 1
            continue

        print_summary(manifest, json_output=args.json)
        if limited:
            state = read_cooldown(out)
            if state:
                raise IntakeError(explain_cooldown(state, cooldown_remaining(state)))
            return 3
        return 0 if result.returncode == 0 else 1


def write_manifest(out: Path, manifest: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "intake-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    runs = out / "runs.jsonl"
    summary = {key: manifest.get(key) for key in ("run_id", "started_at_utc", "finished_at_utc", "target", "target_mode", "gallery_dl_returncode", "counts")}
    summary["hints"] = manifest.get("hints", [])
    with runs.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")


def print_summary(manifest: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return
    counts = manifest["counts"]
    print(f"target        : {manifest['target']} ({manifest['target_mode']})")
    print(f"archive       : {manifest['archive_dir']}")
    print(f"inbox         : {manifest['inbox_dir']}")
    print(f"posts         : {counts['posts_in_archive']} in archive, {counts['posts_staged_this_run']} newly staged, {counts['posts_skipped']} skipped")
    print(f"media         : {counts['images']} image(s), {counts['videos']} video(s) newly staged")
    print(f"gallery-dl    : exit {manifest['gallery_dl_returncode']}")
    for warning in manifest.get("warnings", [])[:10]:
        print(f"! {warning}")
    for hint in manifest.get("hints", []):
        print(f"! {hint}")
    if counts["posts_in_archive"] == 0:
        print("Nothing reached the archive. If the target is saved/private, check the session cookies.")


def command_project(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser().resolve()
    archive_dir = Path(args.archive).expanduser().resolve() if args.archive else out / "_archive"
    inbox = Path(args.inbox).expanduser().resolve() if args.inbox else out / "_inbox"
    if not archive_dir.is_dir():
        raise IntakeError(f"Archive directory not found: {archive_dir}")
    items = read_sidecars(archive_dir)
    records, skipped, warnings = project(
        items,
        inbox,
        mode_label=args.mode or "project",
        acquisition=f"Re-projection from existing archive ({TOOL} {VERSION})",
        images_only=args.images_only,
    )
    manifest = {
        "tool": TOOL,
        "version": VERSION,
        "run_id": run_id(),
        "started_at_utc": iso_now(),
        "finished_at_utc": iso_now(),
        "target": None,
        "target_mode": "project",
        "auth": {"method": "none"},
        "command": ["project"],
        "archive_dir": str(archive_dir),
        "inbox_dir": str(inbox),
        "gallery_dl_returncode": 0,
        "counts": {
            "posts_in_archive": len(items),
            "posts_staged_this_run": len(records),
            "posts_skipped": len(skipped),
            "images": sum(record["images"] for record in records),
            "videos": sum(record["videos"] for record in records),
        },
        "items": records,
        "skipped": skipped,
        "warnings": warnings,
        "hints": [],
    }
    write_manifest(out, manifest)
    print_summary(manifest, json_output=args.json)
    return 0


def command_status(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser().resolve()
    state = read_cooldown(out)
    remaining = cooldown_remaining(state)
    archive_dir = out / "_archive"
    items = read_sidecars(archive_dir) if archive_dir.is_dir() else []
    inbox = scan_inbox(out / "_inbox")
    payload = {
        "tool": TOOL,
        "version": VERSION,
        "out": str(out),
        "cooldown": state,
        "cooldown_remaining_seconds": int(max(0, remaining)),
        "cooldown_active": remaining > 0,
        "archive_posts": len(items),
        "inbox_items": len(inbox),
        "ladder_seconds": list(COOLDOWN_LADDER_SECONDS),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"working root : {out}")
        if remaining > 0 and state:
            print(f"cooldown     : ACTIVE, level {state.get('level')}, {int(remaining)}s left (until {state.get('until')})")
            print(f"reason       : {state.get('reason')}")
        elif state:
            print(f"cooldown     : expired (level {state.get('level')} at {state.get('until')})")
        else:
            print("cooldown     : none")
        print(f"archive      : {payload['archive_posts']} post(s)")
        print(f"inbox        : {payload['inbox_items']} item dir(s)")
        print(f"ladder       : {', '.join(f'{seconds // 60}m' for seconds in COOLDOWN_LADDER_SECONDS)}")
    return 0


def command_check(args: argparse.Namespace, gdl: GalleryDL) -> int:
    payload: dict[str, Any] = {"tool": TOOL, "version": VERSION, "ok": True, "checks": []}
    try:
        binary = gdl.resolve()
        payload["checks"].append({"check": "gallery-dl binary", "ok": True, "detail": binary})
        payload["gallery_dl"] = gdl.probe_version()
        payload["checks"].append({"check": "gallery-dl version", "ok": True, "detail": payload["gallery_dl"]})
    except IntakeError as error:
        payload["ok"] = False
        payload["checks"].append({"check": "gallery-dl binary", "ok": False, "detail": str(error)})

    cookies_file = Path(args.cookies).expanduser() if args.cookies else None
    if args.cookies_from_browser:
        payload["checks"].append({"check": "auth", "ok": True, "detail": f"cookies-from-browser: {browser_spec(args.cookies_from_browser, getattr(args, 'keyring', None))} (verified on first plan)"})
    elif cookies_file:
        exists = cookies_file.is_file()
        payload["checks"].append({"check": "auth", "ok": exists, "detail": str(cookies_file) if exists else f"missing: {cookies_file}"})
        payload["ok"] = payload["ok"] and exists
    else:
        payload["checks"].append({"check": "auth", "ok": False, "detail": "no cookies supplied; only public targets will work"})
        payload["ok"] = False

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for check in payload["checks"]:
            print(f"{'ok  ' if check['ok'] else 'FAIL'} {check['check']}: {check['detail']}")
    return 0 if payload["ok"] else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gallery-dl", default=os.environ.get("GALLERY_DL_BINARY", "gallery-dl"), help="gallery-dl binary (default: PATH lookup)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def auth_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--cookies", help="Netscape cookies.txt for a logged-in Instagram session")
        sub.add_argument("--cookies-from-browser", help="firefox|chrome|chromium|brave|edge (uses that browser's session)")
        sub.add_argument("--keyring", choices=["gnomekeyring", "kwallet", "basictext"], help="Chromium cookie keyring backend (Linux); needed when the shell has no desktop env")
        sub.add_argument("--sleep-request", default="3.0-6.0", help="seconds between HTTP requests (default 3.0-6.0)")
        sub.add_argument("--sleep-429", default="60-120", help="seconds to wait on HTTP 429 inside gallery-dl (default 60-120)")
        sub.add_argument("--timeout", type=int, default=3600, help="subprocess timeout in seconds")

    check = subparsers.add_parser("check", help="preflight: tool and auth availability")
    check.add_argument("--cookies")
    check.add_argument("--cookies-from-browser")
    check.add_argument("--keyring", choices=["gnomekeyring", "kwallet", "basictext"])

    plan = subparsers.add_parser("plan", help="enumerate a target without downloading media")
    plan.add_argument("--target", required=True, help="Instagram URL")
    plan.add_argument("--out", default="~/Documents/knowledge-vault/Inbox/media/_instagram", help="working root (config + archive)")
    plan.add_argument("--max-posts", type=int, default=30, help="sample size in media files (default 30)")
    plan.add_argument("--all", dest="max_posts", action="store_const", const=0, help="enumerate the whole target (metadata only, slower)")
    auth_options(plan)

    fetch = subparsers.add_parser("fetch", help="download into the archive and project the inbox")
    fetch.add_argument("--target", required=True, help="Instagram URL")
    fetch.add_argument("--out", default="~/Documents/knowledge-vault/Inbox/media/_instagram", help="working root")
    fetch.add_argument("--max-posts", type=int, default=200, help="cap posts this run (default 200; use --all for the whole folder)")
    fetch.add_argument("--all", dest="max_posts", action="store_const", const=0, help="no post cap — collect the whole folder")
    fetch.add_argument("--images-only", action="store_true", help="skip videos/reels")
    fetch.add_argument("--no-archive", action="store_true", help="do not record a download archive (breaks resume)")
    fetch.add_argument("--retries", type=int, default=4)
    fetch.add_argument("--run-id", help="label this run in the manifest")
    fetch.add_argument("--auto-retry", type=int, default=0, metavar="N", help="after a 429, wait out the cooldown and resume, up to N extra attempts")
    fetch.add_argument("--max-wait", type=int, default=3600, metavar="SECONDS", help="longest single cooldown wait to sit through (default 3600)")
    fetch.add_argument("--force", action="store_true", help="run despite an active cooldown (not recommended)")
    auth_options(fetch)

    status = subparsers.add_parser("status", help="show cooldown state and archive counts")
    status.add_argument("--out", default="~/Documents/knowledge-vault/Inbox/media/_instagram", help="working root")

    project_cmd = subparsers.add_parser("project", help="re-project the inbox from an existing archive (no network)")
    project_cmd.add_argument("--out", default="~/Documents/knowledge-vault/Inbox/media/_instagram")
    project_cmd.add_argument("--archive", help="archive directory (default: OUT/_archive)")
    project_cmd.add_argument("--inbox", help="inbox directory (default: OUT/_inbox)")
    project_cmd.add_argument("--images-only", action="store_true")
    project_cmd.add_argument("--mode", help="label recorded in source.md acquisition line")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    gdl = GalleryDL(binary=args.gallery_dl)
    try:
        if args.command == "check":
            return command_check(args, gdl)
        if args.command == "plan":
            return command_plan(args, gdl)
        if args.command == "fetch":
            return command_fetch(args, gdl)
        if args.command == "project":
            return command_project(args)
        if args.command == "status":
            return command_status(args)
    except IntakeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
