#!/usr/bin/env python3
"""Tests for the media-intake staging stage.

Run: .venv/bin/python -m unittest discover -s skill/scripts -p 'test_*.py' -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import intake_ig as intake  # noqa: E402

MEDIA_OCR_SCRIPTS = Path(__file__).resolve().parents[2].parent / "media-ocr" / "skill" / "scripts"


def post_kwdict(**overrides):
    """A kwdict shaped exactly like gallery-dl's Instagram _parse_post output."""
    base = {
        "category": "instagram",
        "subcategory": "saved",
        "post_shortcode": "DEF456",
        "post_id": "3400123456789",
        "post_url": "https://www.instagram.com/p/DEF456/",
        "username": "creator",
        "fullname": "Creator Name",
        "description": "A caption about\nsourdough and other things",
        "post_date": "2026-03-14 09:12:00",
        "date": "2026-03-14 09:12:00",
        "type": "post",
        "count": 2,
        "num": 1,
        "extension": "jpg",
        "media_id": "111",
        "filename": "01_DEF456",
    }
    base.update(overrides)
    return base


class ClassifyTargetTests(unittest.TestCase):
    def test_saved_all_posts(self):
        target = intake.classify_target("https://www.instagram.com/mick/saved/all-posts/")
        self.assertEqual(target["mode"], "saved")
        self.assertEqual(target["user"], "mick")

    def test_saved_short_form(self):
        self.assertEqual(intake.classify_target("https://www.instagram.com/mick/saved/")["mode"], "saved")

    def test_named_collection(self):
        target = intake.classify_target("https://www.instagram.com/mick/saved/Recipes/17895695668004550/?hl=en")
        self.assertEqual(target["mode"], "collection")
        self.assertEqual(target["collection"], "Recipes")
        self.assertEqual(target["collection_id"], "17895695668004550")

    def test_profile_sections(self):
        self.assertEqual(intake.classify_target("https://instagram.com/someone/")["mode"], "profile")
        self.assertEqual(intake.classify_target("https://www.instagram.com/someone/reels/")["mode"], "profile")

    def test_post_and_tag(self):
        self.assertEqual(intake.classify_target("https://www.instagram.com/p/DEF456/")["shortcode"], "DEF456")
        self.assertEqual(intake.classify_target("https://www.instagram.com/reel/DEF456/")["mode"], "post")
        self.assertEqual(intake.classify_target("https://www.instagram.com/explore/tags/sourdough/")["mode"], "tag")

    def test_unsupported_url_is_explicit(self):
        with self.assertRaises(intake.IntakeError):
            intake.classify_target("https://imgur.com/gallery/hC8XJ0s")


class ParsingTests(unittest.TestCase):
    def test_whole_document_json(self):
        """gallery-dl pretty-prints one JSON array unless output.jsonl is set."""
        stdout = json.dumps([[-1, {"error": "AbortExtraction", "message": "HTTP redirect to login page"}], [3, "https://x/a.jpg", post_kwdict()]], indent=2)
        rows = intake.parse_json_lines(stdout)
        self.assertEqual(len(rows), 2)
        self.assertEqual(intake.collect_errors(rows)[0]["error"], "AbortExtraction")

    def test_json_lines_mode(self):
        stdout = "\n".join((json.dumps([3, "https://x/a.jpg", post_kwdict()]), json.dumps([3, "https://x/b.jpg", post_kwdict(num=2)])))
        rows = intake.parse_json_lines(stdout)
        self.assertEqual(len(rows), 2)

    def test_login_redirect_is_reported_not_swallowed(self):
        """gallery-dl exits 0 on AbortExtraction, so exit status alone is a lie."""
        errors = intake.collect_errors([[-1, {"error": "AbortExtraction", "message": "HTTP redirect to login page"}]])
        hints = intake.diagnose_failure("", 0, errors)
        self.assertTrue(any("cookies" in hint for hint in hints))

    def test_rate_limit_hint(self):
        hints = intake.diagnose_failure("[instagram][error] HttpError: '429 Too Many Requests'", 1)
        self.assertTrue(any("rate-limited" in hint for hint in hints))


class AggregateTests(unittest.TestCase):
    def test_groups_per_post_and_counts_media(self):
        rows = [
            [3, "https://x/1.jpg", post_kwdict(num=1)],
            [3, "https://x/2.jpg", post_kwdict(num=2)],
            [3, "https://x/1.mp4", post_kwdict(post_shortcode="VIDEO1", num=1, type="reel", video_url="https://x/1.mp4")],
        ]
        posts = {post["post_shortcode"]: post for post in intake.aggregate_posts(rows)}
        self.assertEqual(posts["DEF456"]["media_count"], 2)
        self.assertEqual(posts["DEF456"]["kind"], "image")
        self.assertEqual(posts["VIDEO1"]["kind"], "video")
        self.assertEqual(posts["DEF456"]["post_date"], "2026-03-14")


class NamingTests(unittest.TestCase):
    def test_inbox_dir_is_deterministic_and_traceable(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = make_item(Path(tmp), "DEF456")
            first = intake.inbox_dir_name(item)
            second = intake.inbox_dir_name(item)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("2026-03-14_creator_"))
        self.assertTrue(first.endswith("_DEF456"))
        self.assertNotIn(" ", first)
        self.assertNotIn("/", first)

    def test_missing_date_and_caption_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = make_item(Path(tmp), "XYZ999", post_date="", description="")
            name = intake.inbox_dir_name(item)
        self.assertTrue(name.startswith("undated_creator_post_XYZ999"))


class SourceMarkdownTests(unittest.TestCase):
    def test_records_observable_provenance_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = make_item(Path(tmp), "DEF456")
            text = intake.source_markdown(item, mode_label="collection", acquisition="Automated collection (test)", archive_rel="2026-03-14_DEF456", media_summary="2 image(s)")
        self.assertIn("- **Local item ID:**", text)
        self.assertIn("- **Source URL:** https://www.instagram.com/p/DEF456/", text)
        self.assertIn("- **Creator/publisher:** @creator (Creator Name)", text)
        self.assertIn("- **Creator handle observed on slides:** @creator", text)
        self.assertIn("- **Collection:** Recipes", text)
        self.assertIn("not verified", text)

    def test_caption_is_flattened(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = make_item(Path(tmp), "DEF456")
            text = intake.source_markdown(item, mode_label="saved", acquisition="x", archive_rel="y", media_summary="z")
        caption_lines = [line for line in text.splitlines() if line.startswith("- **Caption")]
        self.assertEqual(len(caption_lines), 1)
        self.assertIn("sourdough and other things", caption_lines[0])

    def test_missing_fields_are_omitted_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = make_item(Path(tmp), "NOCAP1", description="", post_url=None, collection=None, location=None, tags=[])
            text = intake.source_markdown(item, mode_label="saved", acquisition="x", archive_rel="y", media_summary="1 image(s)")
        self.assertNotIn("Source URL", text)
        self.assertNotIn("Caption", text)
        self.assertNotIn("Collection", text)

    def test_long_caption_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = make_item(Path(tmp), "LONG1", description="word " * 400)
            text = intake.source_markdown(item, mode_label="saved", acquisition="x", archive_rel="y", media_summary="z")
        caption = next(line for line in text.splitlines() if line.startswith("- **Caption"))
        self.assertLessEqual(len(caption), intake.CAPTION_LIMIT + 40)
        self.assertTrue(caption.endswith("…"))


class ArchiveTests(unittest.TestCase):
    def test_read_sidecars_groups_by_post_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "_archive"
            folder = root / "instagram" / "creator" / "2026-03-14_DEF456"
            folder.mkdir(parents=True)
            for num, name in enumerate(("01_DEF456.jpg", "02_DEF456.jpg"), start=1):
                (folder / name).write_bytes(b"x")
                (folder / f"{name}.json").write_text(json.dumps(post_kwdict(num=num)), encoding="utf-8")
            items = intake.read_sidecars(root)
        self.assertEqual(len(items), 1)
        self.assertEqual([path.name for path in items[0].media], ["01_DEF456.jpg", "02_DEF456.jpg"])

    def test_sidecars_without_shortcode_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stray.json").write_text(json.dumps({"category": "other"}), encoding="utf-8")
            self.assertEqual(intake.read_sidecars(root), [])


class ProjectionTests(unittest.TestCase):
    def test_creates_ordered_files_and_never_renumbers_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_item(root, "DEF456", media_count=3)
            inbox = root / "_inbox"
            records, skipped, _ = intake.project([item], inbox, mode_label="saved", acquisition="test")
            self.assertEqual(len(records), 1)
            staged = sorted(path.name for path in (inbox / records[0]["inbox_dir"]).iterdir() if path.suffix in {".jpg", ".mp4"})
            self.assertEqual(staged, ["01.jpg", "02.jpg", "03.jpg"])

            folder = item.source_dir
            (folder / "04_DEF456.jpg").write_bytes(b"x")
            records2, skipped2, _ = intake.project([make_item(root, "DEF456", media_count=4)], inbox, mode_label="saved", acquisition="test")
            self.assertEqual(records2, [])
            self.assertEqual(skipped2[0]["why"], "already in inbox")

    def test_images_only_skips_video_only_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_item(root, "REEL1", media_count=1, extensions=(".mp4",))
            records, skipped, warnings = intake.project([item], root / "_inbox", mode_label="saved", acquisition="test", images_only=True)
        self.assertEqual(records, [])
        self.assertIn("no image files", skipped[0]["why"])

    def test_video_only_post_flags_ocr_video_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_item(root, "REEL2", media_count=1, extensions=(".mp4",))
            records, _, warnings = intake.project([item], root / "_inbox", mode_label="saved", acquisition="test")
            meta = json.loads((root / "_inbox" / records[0]["inbox_dir"] / ".intake.json").read_text())
        self.assertEqual(meta["ocr_entry_point"], "scripts/ocr_video.py")
        self.assertTrue(any("ocr_video.py" in warning for warning in warnings))

    def test_hardlink_not_copy_when_possible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_item(root, "DEF456", media_count=1)
            records, _, _ = intake.project([item], root / "_inbox", mode_label="saved", acquisition="test")
            staged = root / "_inbox" / records[0]["inbox_dir"] / "01.jpg"
            self.assertEqual(staged.stat().st_ino, item.media[0].stat().st_ino)


@unittest.skipUnless((MEDIA_OCR_SCRIPTS / "ocr_batch.py").is_file(), "media-ocr sibling project not present")
class MediaOcrContractTests(unittest.TestCase):
    """The generated source.md must be readable by media-ocr's own parser."""

    def load_parse_source(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("ocr_batch_contract", MEDIA_OCR_SCRIPTS / "ocr_batch.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.parse_source

    def test_media_ocr_parses_every_field(self):
        parse_source = self.load_parse_source()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_item(root, "DEF456")
            records, _, _ = intake.project([item], root / "_inbox", mode_label="collection", acquisition="Automated collection (test)")
            source_path = root / "_inbox" / records[0]["inbox_dir"] / "source.md"
            parsed = parse_source(source_path)
        self.assertEqual(parsed["Source URL"], "https://www.instagram.com/p/DEF456/")
        self.assertEqual(parsed["Creator handle observed on slides"], "@creator")
        self.assertEqual(parsed["Collection"], "Recipes")
        self.assertIn("sourdough", parsed["Caption (as delivered)"])
        self.assertIn("not verified", parsed["Rights/attribution uncertainty"])
        self.assertEqual(parsed["Visible date"], "2026-03-14")

    def test_media_ocr_finds_staged_images(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("ocr_batch_images", MEDIA_OCR_SCRIPTS / "ocr_batch.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_item(root, "DEF456", media_count=3)
            records, _, _ = intake.project([item], root / "_inbox", mode_label="saved", acquisition="test")
            found = module.image_paths(root / "_inbox" / records[0]["inbox_dir"])
        self.assertEqual([path.name for path in found], ["01.jpg", "02.jpg", "03.jpg"])


def make_item(root: Path, shortcode: str, *, media_count: int = 2, extensions: tuple[str, ...] = (".jpg",), post_date: str = "2026-03-14", description: str = "A caption about\nsourdough and other things", **overrides) -> intake.StagedItem:
    folder = root / "archive" / f"2026-03-14_{shortcode}"
    folder.mkdir(parents=True, exist_ok=True)
    media = []
    for index in range(1, media_count + 1):
        extension = extensions[(index - 1) % len(extensions)]
        path = folder / f"{index:02d}_{shortcode}{extension}"
        path.write_bytes(b"media")
        media.append(path)
    return intake.StagedItem(
        post_shortcode=shortcode,
        post_url=overrides.get("post_url", f"https://www.instagram.com/p/{shortcode}/"),
        username=overrides.get("username", "creator"),
        fullname=overrides.get("fullname", "Creator Name"),
        post_date=post_date,
        caption=intake.md_value(description),
        collection=overrides.get("collection", "Recipes"),
        location=overrides.get("location"),
        tags=overrides.get("tags", ["sourdough", "bread"]),
        kind=overrides.get("kind", "image"),
        source_dir=folder,
        media=media,
    )


if __name__ == "__main__":
    unittest.main()
