from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from igrep.index import Searcher, build_index, render_line_matches, verify_regex
from igrep.storage import decode_postings, encode_postings, intersect_postings
from igrep.trigram import extract_required_literals, unique_trigram_hashes


class TrigramTests(unittest.TestCase):
    def test_unique_trigram_hashes(self) -> None:
        hashes = unique_trigram_hashes("banana")
        self.assertEqual(len(hashes), 3)
        self.assertEqual(hashes, sorted(set(hashes)))

    def test_extract_required_literals(self) -> None:
        literals = extract_required_literals(r"foo.*bar(?:baz)?qux")
        self.assertEqual(literals, ["foo", "bar", "qux"])


class PostingTests(unittest.TestCase):
    def test_posting_roundtrip(self) -> None:
        encoded = encode_postings([1, 4, 7, 10])
        self.assertEqual(decode_postings(encoded), [1, 4, 7, 10])

    def test_posting_intersection(self) -> None:
        self.assertEqual(intersect_postings([1, 2, 4, 8], [2, 3, 4, 9]), [2, 4])


class SearcherTests(unittest.TestCase):
    def test_lookup_roundtrip_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            index_dir = Path(tmp) / "index"
            root.mkdir()
            (root / "a.txt").write_text("alpha beta gamma\n", encoding="utf-8")
            (root / "b.txt").write_text("alpha delta epsilon\n", encoding="utf-8")
            build_index(str(root), str(index_dir))

            searcher = Searcher(str(index_dir), str(root))
            try:
                outcome = searcher.search(r"alpha.*gamma")
                self.assertEqual(outcome.matches, ["a.txt"])
                entry = searcher.lookup.find(unique_trigram_hashes("alpha")[0])
                self.assertIsNotNone(entry)
                self.assertGreater(entry.docfreq, 0)
            finally:
                searcher.close()

    def test_regex_verification_correctness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            index_dir = Path(tmp) / "index"
            root.mkdir()
            (root / "foo.txt").write_text("foo middle BAR\n", encoding="utf-8")
            (root / "bar.txt").write_text("only bar here\n", encoding="utf-8")
            (root / "none.txt").write_text("something else\n", encoding="utf-8")
            build_index(str(root), str(index_dir))

            searcher = Searcher(str(index_dir), str(root))
            try:
                self.assertEqual(searcher.search(r"foo.*bar", ignore_case=True).matches, ["foo.txt"])
                foo_bar_outcome = searcher.search(r"foo|bar")
                self.assertEqual(sorted(foo_bar_outcome.matches), ["bar.txt", "foo.txt"])
                self.assertFalse(foo_bar_outcome.stats.fallback_scan)
                self.assertTrue(searcher.search(r"\w+").stats.fallback_scan)
            finally:
                searcher.close()

    def test_render_line_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            index_dir = Path(tmp) / "index"
            root.mkdir()
            (root / "a.txt").write_text("alpha\nbeta\nalpha beta\n", encoding="utf-8")
            build_index(str(root), str(index_dir))
            searcher = Searcher(str(index_dir), str(root))
            try:
                outcome = searcher.search(r"alpha")
                lines = list(
                    render_line_matches(
                        root=searcher.root,
                        relative_paths=outcome.matches,
                        pattern=r"alpha",
                        include_line_numbers=True,
                    )
                )
                self.assertEqual(lines, ["a.txt:1:alpha", "a.txt:3:alpha beta"])
            finally:
                searcher.close()

    def test_incremental_reuses_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            index_dir = Path(tmp) / "index"
            root.mkdir()
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("alpha beta gamma\n", encoding="utf-8")
            second.write_text("delta epsilon zeta\n", encoding="utf-8")
            build_index(str(root), str(index_dir))

            second.write_text("delta epsilon theta\n", encoding="utf-8")
            stats = build_index(str(root), str(index_dir), incremental=True)
            self.assertEqual(stats.reused_files, 1)
            self.assertEqual(stats.indexed_files, 1)


class VerificationTests(unittest.TestCase):
    def test_verify_regex(self) -> None:
        self.assertTrue(verify_regex(r"foo.*bar", "foo x bar"))
        self.assertFalse(verify_regex(r"foo.*bar", "foo only"))


if __name__ == "__main__":
    unittest.main()
