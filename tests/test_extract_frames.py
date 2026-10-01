"""Fallback extraction and draft materialization regressions; local media only."""
import contextlib
import io
import os
import shutil
import sys
import tempfile

from _helpers import make_video, still_image, write_png
import extract_frames as ef


def test_materialize_counts_only_replaced_images_and_refreshes_tagged():
    d = tempfile.mkdtemp(prefix="vs_materialize_")
    try:
        imgdir = os.path.join(d, "images")
        os.makedirs(imgdir)
        write_png(os.path.join(imgdir, "shot_00_00_10.png"), still_image())
        book, tagged = os.path.join(d, "book.md"), os.path.join(d, "book.tagged.md")
        draft = "new text\n![a](SCREENSHOT:00:00:10)\n![b](SCREENSHOT:00:00:20)\n"
        with open(book, "w", encoding="utf-8") as f:
            f.write(draft)
        with open(tagged, "w", encoding="utf-8") as f:
            f.write("old text")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ef.materialize(book, tagged, imgdir)
        assert "replaced 1 placeholders" in buf.getvalue(), buf.getvalue()
        assert open(tagged, encoding="utf-8").read() == draft
        content = open(book, encoding="utf-8").read()
        assert "images/shot_00_00_10.png" in content
        assert "SCREENSHOT:00:00:20" in content
        assert ef.requested_timestamps(content) == ["00:00:10", "00:00:20"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_materialize_no_images_does_not_claim_replacements_or_overwrite_tagged():
    d = tempfile.mkdtemp(prefix="vs_materialize_")
    try:
        book, tagged = os.path.join(d, "book.md"), os.path.join(d, "book.tagged.md")
        with open(book, "w", encoding="utf-8") as f:
            f.write("![a](SCREENSHOT:00:00:10)")
        with open(tagged, "w", encoding="utf-8") as f:
            f.write("old draft")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ef.materialize(book, tagged, d)
        assert "no image links replaced" in buf.getvalue()
        assert open(tagged, encoding="utf-8").read() == "old draft"
        assert not os.path.exists(book + ".partial")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ffmpeg_out_of_range_exit_zero_does_not_replace_old_image_or_materialize():
    d = tempfile.mkdtemp(prefix="vs_extract_")
    original = ef.__file__, sys.argv
    try:
        ef.__file__ = os.path.join(d, "src", "extract_frames.py")
        vdir = os.path.join(d, "output", "BVtest1")
        imgdir = os.path.join(vdir, "images")
        os.makedirs(imgdir)
        src = os.path.join(vdir, "video_source.mp4")
        make_video(src, seconds=1, kind="still")
        book = os.path.join(vdir, "book.md")
        draft = "![a](SCREENSHOT:00:00:10)"
        with open(book, "w", encoding="utf-8") as f:
            f.write(draft)
        out = os.path.join(imgdir, "shot_00_00_10.png")
        write_png(out, still_image())
        before = open(out, "rb").read()
        sys.argv = ["extract_frames.py", "BVtest1"]
        try:
            ef.main()
            raise AssertionError("out-of-range extraction should fail")
        except SystemExit as exc:
            assert "frame extraction failed" in str(exc), exc
        assert open(out, "rb").read() == before
        assert open(book, encoding="utf-8").read() == draft
        assert not os.path.exists(out[:-4] + ".partial.png")
    finally:
        ef.__file__, sys.argv = original
        shutil.rmtree(d, ignore_errors=True)
