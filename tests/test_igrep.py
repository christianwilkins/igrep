from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

from igrep.cli import main as cli_main
from igrep.index import (
    Searcher,
    build_index,
    detect_git_changed_paths,
    detect_state_changes,
    render_line_matches,
    scan_file_state,
    verify_regex,
)
from igrep.storage import decode_postings, encode_postings, intersect_postings
from igrep.trigram import extract_literal_groups, extract_required_literals, unique_trigram_hashes


class TrigramTests(unittest.TestCase):
    def test_unique_trigram_hashes(self) -> None:
        hashes = unique_trigram_hashes("banana")
        self.assertEqual(len(hashes), 3)
        self.assertEqual(hashes, sorted(set(hashes)))

    def test_extract_required_literals(self) -> None:
        literals = extract_required_literals(r"foo.*bar(?:baz)?qux")
        self.assertEqual(literals, ["foo", "bar", "qux"])

    def test_extract_literal_groups_with_shared_prefix(self) -> None:
        groups = extract_literal_groups(r"build_index|bench_index")
        self.assertEqual(groups, [["build_index"], ["bench_index"]])


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

                prefix_or = searcher.search(r"foo middle BAR|only bar here", ignore_case=True)
                self.assertEqual(sorted(prefix_or.matches), ["bar.txt", "foo.txt"])
                self.assertFalse(prefix_or.stats.fallback_scan)

                self.assertTrue(searcher.search(r"\w+").stats.fallback_scan)
            finally:
                searcher.close()

    def test_parallel_verification_matches_single_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            index_dir = Path(tmp) / "index"
            root.mkdir()
            for index in range(20):
                (root / f"file-{index}.txt").write_text(f"alpha {index} beta\n", encoding="utf-8")
            (root / "needle.txt").write_text("prefix needle suffix\n", encoding="utf-8")
            build_index(str(root), str(index_dir))

            searcher = Searcher(str(index_dir), str(root))
            try:
                one_thread = searcher.search(r"needle", jobs=1)
                many_threads = searcher.search(r"needle", jobs=4)
                self.assertEqual(one_thread.matches, ["needle.txt"])
                self.assertEqual(many_threads.matches, one_thread.matches)
                self.assertEqual(many_threads.stats.verified_files, one_thread.stats.verified_files)
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
            self.assertEqual(stats.deleted_files, 0)

    def test_incremental_changed_paths(self) -> None:
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
            stats = build_index(
                str(root),
                str(index_dir),
                incremental=True,
                changed_paths={"second.txt"},
            )
            self.assertEqual(stats.reused_files, 1)
            self.assertEqual(stats.indexed_files, 1)
            self.assertEqual(stats.deleted_files, 0)

            first.unlink()
            stats = build_index(
                str(root),
                str(index_dir),
                incremental=True,
                changed_paths={"first.txt"},
            )
            self.assertEqual(stats.deleted_files, 1)


class StateAndGitTests(unittest.TestCase):
    def test_detect_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            index_dir = Path(tmp) / "index"
            root.mkdir()
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            before = scan_file_state(root=root, index_dir=index_dir)
            (root / "a.txt").write_text("two\n", encoding="utf-8")
            (root / "b.txt").write_text("three\n", encoding="utf-8")
            after = scan_file_state(root=root, index_dir=index_dir)
            changed = detect_state_changes(before, after)
            self.assertIn("a.txt", changed)
            self.assertIn("b.txt", changed)

    def test_detect_git_changed_paths(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "tester"], cwd=root, check=True)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)

            (root / "a.txt").write_text("two\n", encoding="utf-8")
            (root / "b.txt").write_text("three\n", encoding="utf-8")
            changed = detect_git_changed_paths(root)
            self.assertIsNotNone(changed)
            assert changed is not None
            self.assertIn("a.txt", changed)
            self.assertIn("b.txt", changed)


class CLITests(unittest.TestCase):
    def test_rg_wrapper_returns_match_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            index_dir = root / ".igrep"
            root.mkdir()
            (root / "a.txt").write_text("hello world\n", encoding="utf-8")
            build_index(str(root), str(index_dir))
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main(["rg", "hello", str(root), "--index-dir", str(index_dir), "-n"])
            self.assertEqual(code, 0)

    def test_rg_wrapper_returns_no_match_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            index_dir = root / ".igrep"
            root.mkdir()
            (root / "a.txt").write_text("hello world\n", encoding="utf-8")
            build_index(str(root), str(index_dir))
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main(["rg", "goodbye", str(root), "--index-dir", str(index_dir)])
            self.assertEqual(code, 1)


class VerificationTests(unittest.TestCase):
    def test_verify_regex(self) -> None:
        self.assertTrue(verify_regex(r"foo.*bar", "foo x bar"))
        self.assertFalse(verify_regex(r"foo.*bar", "foo only"))


if __name__ == "__main__":
    unittest.main()
