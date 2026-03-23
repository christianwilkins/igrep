from __future__ import annotations

import argparse
import sys

from .index import Searcher, bench_index, build_index, format_bench_table, render_line_matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="igrep")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="build or refresh the trigram index")
    build_parser.add_argument("--root", required=True)
    build_parser.add_argument("--index-dir", required=True)
    build_parser.add_argument("--incremental", action="store_true")

    search_parser = subparsers.add_parser("search", help="search an existing index")
    search_parser.add_argument("--pattern", required=True)
    search_parser.add_argument("--root")
    search_parser.add_argument("--index-dir", required=True)
    search_parser.add_argument("--ignore-case", action="store_true")
    search_parser.add_argument("--max-files", type=int)
    search_parser.add_argument("--files-only", action="store_true", help="print only matching file paths")
    search_parser.add_argument("--line-numbers", action="store_true", help="include line numbers in output")

    bench_parser = subparsers.add_parser("bench", help="benchmark igrep against ripgrep")
    bench_parser.add_argument("--root", required=True)
    bench_parser.add_argument("--index-dir", required=True)

    args = parser.parse_args(argv)

    if args.command == "build":
        stats = build_index(root=args.root, index_dir=args.index_dir, incremental=args.incremental)
        print(
            f"indexed={stats.indexed_files} reused={stats.reused_files} "
            f"skipped={stats.skipped_files} trigram_terms={stats.trigram_terms}"
        )
        return 0

    if args.command == "search":
        searcher = Searcher(index_dir=args.index_dir, root=args.root)
        try:
            outcome = searcher.search(pattern=args.pattern, ignore_case=args.ignore_case, max_files=args.max_files)
            if args.files_only:
                lines = outcome.matches
            else:
                lines = list(
                    render_line_matches(
                        root=searcher.root,
                        relative_paths=outcome.matches,
                        pattern=args.pattern,
                        ignore_case=args.ignore_case,
                        include_line_numbers=args.line_numbers,
                    )
                )
        finally:
            searcher.close()
        if outcome.stats.warning:
            print(f"warning: {outcome.stats.warning}", file=sys.stderr)
        for line in lines:
            print(line)
        print(
            f"matched_files={outcome.stats.matched_files} candidates={outcome.stats.candidate_files} "
            f"verified={outcome.stats.verified_files} printed={len(lines)}",
            file=sys.stderr,
        )
        return 0

    if args.command == "bench":
        rows = bench_index(root=args.root, index_dir=args.index_dir)
        print(format_bench_table(rows))
        return 0

    raise AssertionError("unreachable")

