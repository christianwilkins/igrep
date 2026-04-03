from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys
import time

from .index import (
    Searcher,
    bench_index,
    build_index,
    detect_git_changed_paths,
    detect_state_changes,
    format_bench_table,
    render_line_matches,
    scan_file_state,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="igrep")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="build or refresh the trigram index")
    build_parser.add_argument("--root", required=True)
    build_parser.add_argument("--index-dir", required=True)
    build_parser.add_argument("--incremental", action="store_true")
    build_parser.add_argument("--git-aware", action="store_true", help="only reindex files changed in git status")

    search_parser = subparsers.add_parser("search", help="search an existing index")
    search_parser.add_argument("--pattern", required=True)
    search_parser.add_argument("--root")
    search_parser.add_argument("--index-dir", required=True)
    search_parser.add_argument("--ignore-case", action="store_true")
    search_parser.add_argument("--max-files", type=int)
    search_parser.add_argument("--files-only", action="store_true", help="print only matching file paths")
    search_parser.add_argument("--line-numbers", action="store_true", help="include line numbers in output")
    search_parser.add_argument("--json", action="store_true", help="emit machine readable JSON output")
    search_parser.add_argument("--jobs", type=int, default=1, help="parallel verification worker count")
    search_parser.add_argument("--auto-build", action="store_true", help="run incremental build before search")

    rg_parser = subparsers.add_parser("rg", help="ripgrep style wrapper for common flags")
    rg_parser.add_argument("pattern", nargs="?")
    rg_parser.add_argument("path", nargs="?", default=".")
    rg_parser.add_argument("-e", "--regexp", action="append", help="pattern; may be repeated")
    rg_parser.add_argument("-i", "--ignore-case", action="store_true")
    rg_parser.add_argument("-n", "--line-number", action="store_true")
    rg_parser.add_argument("-l", "--files-with-matches", action="store_true")
    rg_parser.add_argument("-m", "--max-count", type=int)
    rg_parser.add_argument("--index-dir", default=".igrep")
    rg_parser.add_argument("--jobs", type=int, default=1)
    rg_parser.add_argument("--auto-build", action="store_true")
    rg_parser.add_argument("--json", action="store_true")

    watch_parser = subparsers.add_parser("watch", help="watch filesystem and keep index fresh")
    watch_parser.add_argument("--root", required=True)
    watch_parser.add_argument("--index-dir", required=True)
    watch_parser.add_argument("--interval", type=float, default=0.75, help="poll interval in seconds")
    watch_parser.add_argument("--debounce", type=float, default=0.20, help="settle delay before rebuilding")
    watch_parser.add_argument("--git-aware", action="store_true", help="merge git status changes into fs changes")

    bench_parser = subparsers.add_parser("bench", help="benchmark igrep against ripgrep")
    bench_parser.add_argument("--root", required=True)
    bench_parser.add_argument("--index-dir", required=True)

    args = parser.parse_args(argv)

    if args.command == "build":
        changed_paths = None
        if args.incremental and args.git_aware:
            changed_paths = detect_git_changed_paths(args.root)
        stats = build_index(
            root=args.root,
            index_dir=args.index_dir,
            incremental=args.incremental,
            changed_paths=changed_paths,
        )
        print(_format_build_stats(stats))
        return 0

    if args.command == "search":
        return _run_search(
            pattern=args.pattern,
            root=args.root,
            index_dir=args.index_dir,
            ignore_case=args.ignore_case,
            max_files=args.max_files,
            files_only=args.files_only,
            line_numbers=args.line_numbers,
            json_output=args.json,
            jobs=args.jobs,
            auto_build=args.auto_build,
        )

    if args.command == "rg":
        patterns = list(args.regexp or [])
        if args.pattern:
            patterns.append(args.pattern)
        if not patterns:
            print("error: provide PATTERN or -e PATTERN", file=sys.stderr)
            return 2
        if len(patterns) == 1:
            pattern = patterns[0]
        else:
            pattern = "|".join(f"(?:{value})" for value in patterns)

        return _run_search(
            pattern=pattern,
            root=args.path,
            index_dir=args.index_dir,
            ignore_case=args.ignore_case,
            max_files=args.max_count,
            files_only=args.files_with_matches,
            line_numbers=args.line_number,
            json_output=args.json,
            jobs=args.jobs,
            auto_build=args.auto_build,
        )

    if args.command == "watch":
        return _run_watch(
            root=args.root,
            index_dir=args.index_dir,
            interval=args.interval,
            debounce=args.debounce,
            git_aware=args.git_aware,
        )

    if args.command == "bench":
        rows = bench_index(root=args.root, index_dir=args.index_dir)
        print(format_bench_table(rows))
        return 0

    raise AssertionError("unreachable")


def _run_search(
    pattern: str,
    root: str | None,
    index_dir: str,
    ignore_case: bool,
    max_files: int | None,
    files_only: bool,
    line_numbers: bool,
    json_output: bool,
    jobs: int,
    auto_build: bool,
) -> int:
    search_root = root or "."
    if auto_build:
        build_index(
            root=search_root,
            index_dir=index_dir,
            incremental=True,
            changed_paths=detect_git_changed_paths(search_root),
        )

    searcher = Searcher(index_dir=index_dir, root=root)
    try:
        outcome = searcher.search(
            pattern=pattern,
            ignore_case=ignore_case,
            max_files=max_files,
            jobs=jobs,
        )
        if files_only:
            lines = outcome.matches
        else:
            lines = list(
                render_line_matches(
                    root=searcher.root,
                    relative_paths=outcome.matches,
                    pattern=pattern,
                    ignore_case=ignore_case,
                    include_line_numbers=line_numbers,
                )
            )
    finally:
        searcher.close()

    if json_output:
        payload = {
            "files": outcome.matches,
            "lines": lines,
            "stats": asdict(outcome.stats),
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        if outcome.stats.warning:
            print(f"warning: {outcome.stats.warning}", file=sys.stderr)
        for line in lines:
            print(line)
        print(
            f"matched_files={outcome.stats.matched_files} candidates={outcome.stats.candidate_files} "
            f"verified={outcome.stats.verified_files} printed={len(lines)}",
            file=sys.stderr,
        )

    return 0 if outcome.stats.matched_files > 0 else 1


def _run_watch(root: str, index_dir: str, interval: float, debounce: float, git_aware: bool) -> int:
    stats = build_index(root=root, index_dir=index_dir, incremental=True, changed_paths=detect_git_changed_paths(root))
    print(_format_build_stats(stats), flush=True)

    previous_state = scan_file_state(root=root, index_dir=index_dir)
    try:
        while True:
            time.sleep(max(0.05, interval))
            current_state = scan_file_state(root=root, index_dir=index_dir)
            changed = detect_state_changes(previous_state, current_state)
            if git_aware:
                changed_from_git = detect_git_changed_paths(root)
                if changed_from_git:
                    changed |= changed_from_git
            if not changed:
                previous_state = current_state
                continue

            if debounce > 0:
                time.sleep(debounce)
                current_state = scan_file_state(root=root, index_dir=index_dir)
                changed |= detect_state_changes(previous_state, current_state)

            stats = build_index(root=root, index_dir=index_dir, incremental=True, changed_paths=changed)
            previous_state = current_state
            print(_format_build_stats(stats), flush=True)
    except KeyboardInterrupt:
        return 130


def _format_build_stats(stats) -> str:
    return (
        f"indexed={stats.indexed_files} reused={stats.reused_files} "
        f"skipped={stats.skipped_files} deleted={stats.deleted_files} "
        f"trigram_terms={stats.trigram_terms}"
    )
