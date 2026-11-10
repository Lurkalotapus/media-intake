#!/usr/bin/env python3
"""Run a whole collection end to end: fetch, then OCR, then report.

Designed for unattended use. Every phase is resumable, because a folder-scale
Instagram pull will hit a rate limit at some point and an OCR pass over thousands
of images will not finish in one sitting.

    run_full.py --out "/path/to/library" --target URL --phase all

Phases
------
fetch   Loop intake_ig until the target is exhausted, sleeping out cooldowns.
ocr     OCR every image-bearing inbox directory that has no draft yet.
all     fetch, then ocr.

Nothing here is destructive: it never deletes media, never overwrites a reviewed
final note, and re-running skips work already done.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
IMAGE_SUFFIXES = {".avif", ".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
DEFAULT_MEDIA_OCR = PROJECT.parent / "media-ocr" / "skill" / "scripts" / "ocr_batch.py"


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def log(message: str, log_path: Path | None = None) -> None:
    line = f"[{stamp()}] {message}"
    print(line, flush=True)
    if log_path:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def cooldown_remaining(out: Path) -> float:
    state = read_json(out / "cooldown.json", None)
    if not isinstance(state, dict) or "until" not in state:
        return 0.0
    try:
        until = datetime.fromisoformat(str(state["until"]))
    except ValueError:
        return 0.0
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return (until - datetime.now(timezone.utc)).total_seconds()


# --------------------------------------------------------------------------
# fetch phase
# --------------------------------------------------------------------------


def fetch_phase(args: argparse.Namespace, out: Path, log_path: Path) -> dict[str, Any]:
    intake = HERE / "intake_ig.py"
    cmd = [
        sys.executable, str(intake), "fetch",
        "--target", args.target,
        "--out", str(out),
        "--max-posts", "0",
        "--auto-retry", "0",  # this loop owns retrying; keep each call single-shot
        "--sleep-request", args.sleep_request,
        "--sleep-429", args.sleep_429,
        "--retries", str(args.retries),
        "--timeout", str(args.timeout),
    ]
    if args.cookies_from_browser:
        cmd += ["--cookies-from-browser", args.cookies_from_browser]
    if args.keyring:
        cmd += ["--keyring", args.keyring]
    if args.cookies:
        cmd += ["--cookies", args.cookies]

    rounds = 0
    stalls = 0
    last_count = -1
    while rounds < args.max_rounds:
        rounds += 1
        wait = cooldown_remaining(out)
        if wait > 0:
            if wait > args.max_wait:
                log(f"cooldown of {int(wait)}s exceeds --max-wait {args.max_wait}s; stopping for now", log_path)
                return {"ok": False, "reason": "cooldown-too-long", "rounds": rounds, "remaining_seconds": int(wait)}
            log(f"waiting {int(wait)}s for rate-limit cooldown before round {rounds}", log_path)
            time.sleep(wait + 5)

        log(f"fetch round {rounds}: starting", log_path)
        result = subprocess.run(cmd, capture_output=True, text=True)
        manifest = read_json(out / "intake-manifest.json", {})
        counts = manifest.get("counts", {})
        archived = counts.get("posts_in_archive", 0)
        limited = bool(manifest.get("rate_limited"))
        log(
            f"fetch round {rounds}: exit {result.returncode}, {archived} post(s) in archive, "
            f"{counts.get('images', 0)} image(s) / {counts.get('videos', 0)} video(s) staged this run, "
            f"rate_limited={limited}",
            log_path,
        )

        if result.returncode == 0 and not limited:
            log("fetch phase complete", log_path)
            return {"ok": True, "rounds": rounds, "posts_in_archive": archived, "counts": counts}

        if limited:
            stalls = 0
            continue

        # Auth or capability failure: looping will not fix it.
        combined = (result.stderr or "") + (result.stdout or "")
        auth_markers = ("cookies", "login page", "AbortExtraction", "unauthori")
        if result.returncode == 2 and any(marker in combined for marker in auth_markers):
            log("fetch aborted: authentication problem. Not retrying.", log_path)
            return {"ok": False, "reason": "auth", "rounds": rounds, "stderr_tail": combined[-1200:]}

        if archived == last_count:
            stalls += 1
        else:
            stalls = 0
        last_count = archived
        if stalls >= args.max_stalls:
            log(f"fetch stalled at {archived} post(s) for {stalls} rounds; stopping", log_path)
            return {"ok": False, "reason": "stalled", "rounds": rounds, "posts_in_archive": archived}
        log(f"round {rounds} made no clean finish; retrying after 60s", log_path)
        time.sleep(60)

    return {"ok": False, "reason": "max-rounds", "rounds": rounds}


# --------------------------------------------------------------------------
# OCR phase
# --------------------------------------------------------------------------


def item_dirs(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    return sorted(path for path in inbox.iterdir() if path.is_dir())


def classify_item(directory: Path) -> tuple[int, int]:
    images = videos = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            images += 1
        elif suffix in VIDEO_SUFFIXES:
            videos += 1
    return images, videos


def ocr_one(directory: Path, media_ocr: Path, python: str, threads: int, timeout: int) -> dict[str, Any]:
    output = directory / "ocr-output"
    draft = output / "ocr-draft.md"
    if draft.is_file():
        return {"item": directory.name, "status": "skipped", "why": "draft exists"}
    env = {**os.environ, "OMP_NUM_THREADS": str(threads), "OpenBLAS_NUM_THREADS": str(threads), "MKL_NUM_THREADS": str(threads)}
    try:
        result = subprocess.run(
            [python, str(media_ocr), "--input", str(directory), "--output", str(output)],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"item": directory.name, "status": "timeout"}
    if result.returncode != 0:
        return {"item": directory.name, "status": "failed", "stderr": (result.stderr or "")[-600:]}
    return {"item": directory.name, "status": "ok", "draft": str(draft.relative_to(directory))}


def ocr_phase(args: argparse.Namespace, out: Path, log_path: Path) -> dict[str, Any]:
    inbox = out / "_inbox"
    media_ocr = Path(args.media_ocr or DEFAULT_MEDIA_OCR)
    if not media_ocr.is_file():
        return {"ok": False, "reason": f"media-ocr not found at {media_ocr}"}
    python = args.ocr_python or str(media_ocr.parents[2] / ".venv" / "bin" / "python")

    pending: list[Path] = []
    video_only: list[str] = []
    for directory in item_dirs(inbox):
        images, videos = classify_item(directory)
        if images == 0:
            if videos:
                video_only.append(directory.name)
            continue
        if not (directory / "ocr-output" / "ocr-draft.md").is_file():
            pending.append(directory)

    log(f"ocr phase: {len(pending)} item(s) pending, {len(video_only)} video-only item(s) not OCR'd", log_path)
    results: list[dict[str, Any]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(ocr_one, directory, media_ocr, python, args.ocr_threads, args.ocr_timeout): directory
            for directory in pending
        }
        for future in as_completed(futures):
            outcome = future.result()
            results.append(outcome)
            done += 1
            if done % 25 == 0 or outcome["status"] != "ok":
                log(f"ocr progress {done}/{len(pending)}: last={outcome['item']} {outcome['status']}", log_path)

    ok = [item for item in results if item["status"] == "ok"]
    failed = [item for item in results if item["status"] not in {"ok", "skipped"}]
    log(f"ocr phase complete: {len(ok)} OCR'd, {len(failed)} failed, {len(video_only)} video-only deferred", log_path)
    return {
        "ok": not failed,
        "pending": len(pending),
        "ocr_done": len(ok),
        "failed": failed,
        "video_only_count": len(video_only),
        "video_only_sample": video_only[:10],
    }


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def write_report(out: Path, fetch: dict[str, Any], ocr: dict[str, Any] | None, args: argparse.Namespace) -> Path:
    inbox = out / "_inbox"
    directories = item_dirs(inbox)
    images = videos = 0
    for directory in directories:
        item_images, item_videos = classify_item(directory)
        images += item_images
        videos += item_videos
    drafts = sum(1 for directory in directories if (directory / "ocr-output" / "ocr-draft.md").is_file())

    lines = [
        "---",
        "type: intake-run-report",
        f"generated: {stamp()}",
        f"source_url: {args.target}",
        "---",
        "",
        "# Instagram intake run",
        "",
        "> [!warning] OCR drafts are not verified text",
        "> `ocr-draft.md` is machine output awaiting review. `ocr-output-final/ocr-final.md` is",
        "> deliberately empty until a human compares the draft against the media. A caption or",
        "> handle recorded here is what Instagram delivered, not a confirmed attribution, and",
        "> never a verified quotation.",
        "",
        "## What ran",
        "",
        f"- Target: `{args.target}`",
        f"- Fetch: {'complete' if fetch.get('ok') else 'incomplete — ' + str(fetch.get('reason'))}"
        f" ({fetch.get('rounds', 0)} round(s))",
        f"- OCR: {'complete' if ocr and ocr.get('ok') else 'incomplete' if ocr else 'not requested'}",
        "",
        "## Library contents",
        "",
        "| | |",
        "|---|---|",
        f"| Item directories | {len(directories)} |",
        f"| Image files | {images} |",
        f"| Video files | {videos} |",
        f"| OCR drafts written | {drafts} |",
        "",
        "## Layout",
        "",
        "```text",
        "Instagram/",
        "  _archive/          gallery-dl's own record + archive.sqlite3 (do not edit)",
        "  _inbox/<item>/     one directory per post: 01.jpg, 02.jpg, source.md, .intake.json",
        "                     plus ocr-output/ and ocr-output-final/ once OCR has run",
        "  intake-manifest.json, runs.jsonl, cooldown.json",
        "```",
        "",
        "## Organising notes for whoever curates this",
        "",
        "- `source.md` holds the provenance that survived collection: post URL, creator handle,",
        "  caption as delivered, visible date, and an explicit rights/uncertainty note.",
        "- Media order is preserved by the zero-padded filenames; `01.jpg` is slide one.",
        "- `.intake.json` carries the post shortcode and which OCR entry point applies.",
        "- Video-only items were staged but not OCR'd (see below); they need text-on-video",
        "  sampling rather than image OCR.",
        "- Directory names are `<visible-date>_<creator>_<slug>_<shortcode>`; the shortcode is",
        "  the stable key and the suffix, so renamed directories remain traceable.",
        "",
    ]
    if ocr and ocr.get("video_only_count"):
        lines += [
            f"## Video-only items deferred ({ocr['video_only_count']})",
            "",
            "Reels and clips were downloaded but not OCR'd. Sample:",
            "",
            *[f"- `{name}`" for name in ocr.get("video_only_sample", [])],
            "",
        ]
    if ocr and ocr.get("failed"):
        lines += ["## OCR failures", "", *[f"- `{item['item']}` — {item.get('stderr', item['status'])[:200]}" for item in ocr["failed"][:25]], ""]
    lines += ["## Re-running", "", "```bash", f"python {HERE / 'run_full.py'} --out \"{out}\" --target \"{args.target}\" --phase all", "```", ""]

    path = out / "RUN-REPORT.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_full", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--phase", choices=["fetch", "ocr", "all"], default="all")
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--keyring")
    parser.add_argument("--cookies")
    parser.add_argument("--sleep-request", default="2.5-4.5")
    parser.add_argument("--sleep-429", default="60-120")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=7200, help="per gallery-dl invocation")
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--max-stalls", type=int, default=3)
    parser.add_argument("--max-wait", type=int, default=21600, help="longest cooldown to sit through (6h)")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--ocr-threads", type=int, default=4)
    parser.add_argument("--ocr-timeout", type=int, default=900)
    parser.add_argument("--ocr-python", help="python with rapidocr (defaults to media-ocr's venv)")
    parser.add_argument("--media-ocr", help="path to media-ocr's ocr_batch.py")
    args = parser.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "run.log"
    log(f"=== run_full start: phase={args.phase} out={out}", log_path)

    fetch: dict[str, Any] = {"ok": False, "reason": "not requested"}
    ocr: dict[str, Any] | None = None

    if args.phase in {"fetch", "all"}:
        fetch = fetch_phase(args, out, log_path)
        log(f"fetch result: {json.dumps(fetch)[:400]}", log_path)
        if not fetch.get("ok"):
            log("fetch did not complete; OCR phase will still run over whatever is staged", log_path)

    if args.phase in {"ocr", "all"}:
        ocr = ocr_phase(args, out, log_path)
        log(f"ocr result: {json.dumps(ocr)[:400]}", log_path)

    report = write_report(out, fetch, ocr, args)
    log(f"report written: {report}", log_path)
    log("=== run_full exit", log_path)
    if args.phase == "all" and not fetch.get("ok"):
        return 1
    if ocr and not ocr.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
