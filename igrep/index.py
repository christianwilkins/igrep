from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Iterable

from .storage import LOOKUP_STRUCT, LookupTable, decode_postings, encode_postings, intersect_postings, read_uint64_slice
from .trigram import extract_literal_groups, hash_token, unique_trigram_hashes


INDEX_VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
POSTING_CACHE_MAX_ENTRIES = 8192


@dataclass(frozen=True)
class DocumentRecord:
    doc_id: int
    relpath: str
    size: int
    mtime_ns: int
    term_offset: int
    term_count: int
    term_ci_offset: int
    term_ci_count: int


@dataclass(frozen=True)
class BuildStats:
    indexed_files: int
    reused_files: int
    skipped_files: int
    deleted_files: int
    trigram_terms: int


@dataclass(frozen=True)
class SearchStats:
    candidate_files: int
    verified_files: int
    matched_files: int
    fallback_scan: bool
    warning: str | None


@dataclass(frozen=True)
class SearchOutcome:
    matches: list[str]
    stats: SearchStats


@dataclass(frozen=True)
class BenchRow:
    pattern: str
    igrep_p50_ms: float
    igrep_p95_ms: float
    rg_p50_ms: float
    rg_p95_ms: float
    speedup: float


def build_index(
    root: str,
    index_dir: str,
    incremental: bool = False,
    changed_paths: set[str] | None = None,
) -> BuildStats:
    root_path = Path(root).resolve()
    index_path = Path(index_dir).resolve()
    index_path.mkdir(parents=True, exist_ok=True)

    previous_docs: dict[str, dict[str, int]] = {}
    old_terms = None
    old_terms_ci = None
    if incremental:
        previous_docs, old_terms, old_terms_ci = _load_previous_state(index_path)

    normalized_changed = _normalize_changed_paths(changed_paths)
    if not previous_docs:
        normalized_changed = None

    doc_records: list[DocumentRecord] = []
    doc_terms: list[list[int]] = []
    doc_terms_ci: list[list[int]] = []
    reused_files = 0
    skipped_files = 0
    indexed_files = 0
    deleted_files = 0

    if normalized_changed is not None and incremental:
        try:
            index_relative = index_path.relative_to(root_path).as_posix()
        except ValueError:
            index_relative = None
        candidate_relpaths = sorted(set(previous_docs.keys()) | normalized_changed)
        for relpath in candidate_relpaths:
            relpath = relpath.strip("/")
            if not relpath:
                continue
            if _is_ignored_relpath(relpath, index_relative):
                continue
            previous = previous_docs.get(relpath)
            file_path = root_path / relpath
            if not file_path.exists() or not file_path.is_file():
                if previous is not None:
                    deleted_files += 1
                continue
            if previous is not None and relpath not in normalized_changed:
                assert old_terms is not None
                assert old_terms_ci is not None
                terms = read_uint64_slice(old_terms, previous["term_offset"], previous["term_count"])
                terms_ci = read_uint64_slice(old_terms_ci, previous["term_ci_offset"], previous["term_ci_count"])
                reused_files += 1
                size = int(previous["size"])
                mtime_ns = int(previous["mtime_ns"])
            else:
                stat_result = file_path.stat()
                if stat_result.st_size > MAX_FILE_BYTES:
                    skipped_files += 1
                    continue
                text = _read_text_file(file_path)
                if text is None:
                    skipped_files += 1
                    continue
                terms = unique_trigram_hashes(text)
                terms_ci = unique_trigram_hashes(text.casefold())
                indexed_files += 1
                size = stat_result.st_size
                mtime_ns = stat_result.st_mtime_ns

            doc_terms.append(terms)
            doc_terms_ci.append(terms_ci)
            doc_records.append(
                DocumentRecord(
                    doc_id=len(doc_records),
                    relpath=relpath,
                    size=size,
                    mtime_ns=mtime_ns,
                    term_offset=0,
                    term_count=len(terms),
                    term_ci_offset=0,
                    term_ci_count=len(terms_ci),
                )
            )
    else:
        for relpath, file_path in _iter_files(root_path, index_path):
            stat_result = file_path.stat()
            if stat_result.st_size > MAX_FILE_BYTES:
                skipped_files += 1
                continue
            previous = previous_docs.get(relpath)
            if previous and previous["size"] == stat_result.st_size and previous["mtime_ns"] == stat_result.st_mtime_ns:
                assert old_terms is not None
                assert old_terms_ci is not None
                terms = read_uint64_slice(old_terms, previous["term_offset"], previous["term_count"])
                terms_ci = read_uint64_slice(old_terms_ci, previous["term_ci_offset"], previous["term_ci_count"])
                reused_files += 1
            else:
                text = _read_text_file(file_path)
                if text is None:
                    skipped_files += 1
                    continue
                terms = unique_trigram_hashes(text)
                terms_ci = unique_trigram_hashes(text.casefold())
                indexed_files += 1
            doc_terms.append(terms)
            doc_terms_ci.append(terms_ci)
            doc_records.append(
                DocumentRecord(
                    doc_id=len(doc_records),
                    relpath=relpath,
                    size=stat_result.st_size,
                    mtime_ns=stat_result.st_mtime_ns,
                    term_offset=0,
                    term_count=len(terms),
                    term_ci_offset=0,
                    term_ci_count=len(terms_ci),
                )
            )

    if old_terms is not None:
        old_terms.close()
    if old_terms_ci is not None:
        old_terms_ci.close()

    doc_records = _write_doc_terms(index_path, doc_records, doc_terms, "doc_terms.bin", "term_offset")
    doc_records = _write_doc_terms(index_path, doc_records, doc_terms_ci, "doc_terms_ci.bin", "term_ci_offset")

    postings = _build_postings(doc_terms)
    postings_ci = _build_postings(doc_terms_ci)
    _write_postings_and_lookup(index_path, postings, "postings.bin", "lookup.bin")
    _write_postings_and_lookup(index_path, postings_ci, "postings_ci.bin", "lookup_ci.bin")

    metadata = {
        "version": INDEX_VERSION,
        "root": str(root_path),
        "documents": [asdict(record) for record in doc_records],
    }
    _atomic_write_bytes(index_path / "metadata.json", json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))

    trigram_terms = sum(len(terms) for terms in doc_terms)
    return BuildStats(
        indexed_files=indexed_files,
        reused_files=reused_files,
        skipped_files=skipped_files,
        deleted_files=deleted_files,
        trigram_terms=trigram_terms,
    )


class Searcher:
    def __init__(self, index_dir: str, root: str | None = None) -> None:
        index_path = Path(index_dir).resolve()
        metadata_path = index_path / "metadata.json"
        if not metadata_path.exists():
            raise FileNotFoundError(f"missing index metadata at {metadata_path}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("version") != INDEX_VERSION:
            raise ValueError(f"unsupported index version: {metadata.get('version')}")
        self.index_path = index_path
        self.root = Path(root).resolve() if root else Path(metadata["root"])
        self.documents = [DocumentRecord(**document) for document in metadata["documents"]]
        self.lookup = LookupTable(str(index_path / "lookup.bin"))
        self.lookup_ci = LookupTable(str(index_path / "lookup_ci.bin"))
        self.postings = open(index_path / "postings.bin", "rb")
        self.postings_ci = open(index_path / "postings_ci.bin", "rb")
        self._postings_cache: dict[tuple[int, int], list[int]] = {}
        self._postings_ci_cache: dict[tuple[int, int], list[int]] = {}

    def close(self) -> None:
        self.lookup.close()
        self.lookup_ci.close()
        self.postings.close()
        self.postings_ci.close()

    def search(self, pattern: str, ignore_case: bool = False, max_files: int | None = None, jobs: int = 1) -> SearchOutcome:
        literal_groups = extract_literal_groups(pattern, ignore_case=ignore_case)
        lookup = self.lookup_ci if ignore_case else self.lookup

        branch_candidates: list[set[int]] = []
        for literals in literal_groups:
            trigram_hashes = sorted(
                {
                    hash_token(literal[index : index + 3])
                    for literal in literals
                    for index in range(max(0, len(literal) - 2))
                    if len(literal) >= 3
                }
            )
            if not trigram_hashes:
                continue

            entries = []
            missing = False
            for token_hash in trigram_hashes:
                entry = lookup.find(token_hash)
                if entry is None:
                    missing = True
                    break
                entries.append(entry)
            if missing:
                continue

            entries.sort(key=lambda item: item.docfreq)
            candidate_list = self._load_posting(entries[0], ignore_case=ignore_case)
            for entry in entries[1:]:
                candidate_list = intersect_postings(candidate_list, self._load_posting(entry, ignore_case=ignore_case))
                if not candidate_list:
                    break
            if candidate_list:
                branch_candidates.append(set(candidate_list))

        warning = None
        fallback_scan = False
        if branch_candidates:
            merged = set().union(*branch_candidates)
            candidates = sorted(merged)
        else:
            fallback_scan = True
            warning = "no usable trigram plan extracted; scanning indexed files"
            candidates = list(range(len(self.documents)))

        flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
        regex = re.compile(pattern, flags)

        matches, verified_files = self._verify_candidates(candidates=candidates, regex=regex, jobs=jobs)
        if max_files is not None:
            matches = matches[:max_files]

        return SearchOutcome(
            matches=matches,
            stats=SearchStats(
                candidate_files=len(candidates),
                verified_files=verified_files,
                matched_files=len(matches),
                fallback_scan=fallback_scan,
                warning=warning,
            ),
        )

    def _verify_candidates(self, candidates: list[int], regex: re.Pattern[str], jobs: int) -> tuple[list[str], int]:
        if jobs <= 1 or len(candidates) <= 1:
            matches: list[str] = []
            verified_files = 0
            for doc_id in candidates:
                relpath, verified, matched = self._verify_candidate(doc_id, regex)
                if verified:
                    verified_files += 1
                if matched:
                    matches.append(relpath)
            return matches, verified_files

        workers = max(1, jobs)
        matches = []
        verified_files = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for relpath, verified, matched in executor.map(lambda doc_id: self._verify_candidate(doc_id, regex), candidates):
                if verified:
                    verified_files += 1
                if matched:
                    matches.append(relpath)
        return matches, verified_files

    def _verify_candidate(self, doc_id: int, regex: re.Pattern[str]) -> tuple[str, bool, bool]:
        relpath = self.documents[doc_id].relpath
        file_path = self.root / relpath
        text = _read_text_file(file_path)
        if text is None:
            return relpath, False, False
        return relpath, True, regex.search(text) is not None

    def _load_posting(self, entry, ignore_case: bool) -> list[int]:
        key = (entry.offset, entry.length)
        if ignore_case:
            cache = self._postings_ci_cache
            handle = self.postings_ci
        else:
            cache = self._postings_cache
            handle = self.postings

        cached = cache.get(key)
        if cached is not None:
            return cached

        handle.seek(entry.offset)
        data = handle.read(entry.length)
        decoded = decode_postings(data)
        if len(cache) >= POSTING_CACHE_MAX_ENTRIES:
            cache.clear()
        cache[key] = decoded
        return decoded


def bench_index(root: str, index_dir: str, repetitions: int = 9) -> list[BenchRow]:
    if shutil.which("rg") is None:
        raise RuntimeError("ripgrep executable `rg` is not available in PATH")

    patterns = [
        r"def\s+[A-Za-z_]\w*\(",
        r"class\s+[A-Z][A-Za-z0-9_]*",
        r"TODO|FIXME|NOTE",
        r"build_index|search|bench_index",
        r"metadata\.json|lookup\.bin|postings\.bin",
    ]
    searcher = Searcher(index_dir=index_dir, root=root)
    try:
        rows: list[BenchRow] = []
        for pattern in patterns:
            igrep_times: list[float] = []
            rg_times: list[float] = []
            for _ in range(repetitions):
                started = time.perf_counter()
                searcher.search(pattern)
                igrep_times.append((time.perf_counter() - started) * 1000.0)

                started = time.perf_counter()
                completed = subprocess.run(
                    ["rg", "-l", "-e", pattern, root],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if completed.returncode not in {0, 1}:
                    raise RuntimeError(f"ripgrep exited with {completed.returncode} for pattern {pattern!r}")
                rg_times.append((time.perf_counter() - started) * 1000.0)

            igrep_p50 = _percentile(igrep_times, 0.50)
            igrep_p95 = _percentile(igrep_times, 0.95)
            rg_p50 = _percentile(rg_times, 0.50)
            rg_p95 = _percentile(rg_times, 0.95)
            rows.append(
                BenchRow(
                    pattern=pattern,
                    igrep_p50_ms=igrep_p50,
                    igrep_p95_ms=igrep_p95,
                    rg_p50_ms=rg_p50,
                    rg_p95_ms=rg_p95,
                    speedup=(rg_p50 / igrep_p50) if igrep_p50 else float("inf"),
                )
            )
        return rows
    finally:
        searcher.close()


def format_bench_table(rows: Iterable[BenchRow]) -> str:
    lines = [
        f"{'pattern':42} {'igrep p50':>10} {'igrep p95':>10} {'rg p50':>10} {'rg p95':>10} {'speedup':>9}",
        "-" * 97,
    ]
    rows_list = list(rows)
    for row in rows_list:
        lines.append(
            f"{row.pattern[:42]:42} {row.igrep_p50_ms:10.2f} {row.igrep_p95_ms:10.2f} {row.rg_p50_ms:10.2f} {row.rg_p95_ms:10.2f} {row.speedup:9.2f}x"
        )
    if rows_list:
        avg_igrep = sum(row.igrep_p50_ms for row in rows_list) / len(rows_list)
        avg_rg = sum(row.rg_p50_ms for row in rows_list) / len(rows_list)
        speedup = (avg_rg / avg_igrep) if avg_igrep else float("inf")
        lines.append("-" * 97)
        lines.append(
            f"{'overall mean p50':42} {avg_igrep:10.2f} {'':10} {avg_rg:10.2f} {'':10} {speedup:9.2f}x"
        )
    return "\n".join(lines)


def verify_regex(pattern: str, text: str, ignore_case: bool = False) -> bool:
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    return re.search(pattern, text, flags) is not None


def render_line_matches(
    root: Path,
    relative_paths: Iterable[str],
    pattern: str,
    ignore_case: bool = False,
    include_line_numbers: bool = False,
) -> Iterable[str]:
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    regex = re.compile(pattern, flags)
    for relative_path in relative_paths:
        text = _read_text_file(root / relative_path)
        if text is None:
            continue
        lines = text.splitlines()
        matched_lines: set[int] = set()
        for match in regex.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            matched_lines.add(line_number)
        if not matched_lines and regex.search(text):
            matched_lines.add(1)

        if not matched_lines:
            continue

        for line_number in sorted(matched_lines):
            if line_number - 1 >= len(lines):
                line_text = ""
            else:
                line_text = lines[line_number - 1]
            if include_line_numbers:
                yield f"{relative_path}:{line_number}:{line_text}"
            else:
                yield f"{relative_path}:{line_text}"


def detect_git_changed_paths(root: str | Path) -> set[str] | None:
    root_path = Path(root).resolve()
    if shutil.which("git") is None:
        return None

    inside = subprocess.run(
        ["git", "-C", str(root_path), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None

    status = subprocess.run(
        ["git", "-C", str(root_path), "status", "--porcelain=1", "--untracked-files=all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        return None

    changed: set[str] = set()
    for raw_line in status.stdout.splitlines():
        if not raw_line:
            continue
        line = raw_line[3:] if len(raw_line) > 3 else ""
        if not line:
            continue
        if " -> " in line:
            old_path, new_path = line.split(" -> ", 1)
            changed.add(old_path.replace("\\", "/").strip())
            changed.add(new_path.replace("\\", "/").strip())
        else:
            changed.add(line.replace("\\", "/").strip())

    return _normalize_changed_paths(changed)


def scan_file_state(root: str | Path, index_dir: str | Path) -> dict[str, tuple[int, int]]:
    root_path = Path(root).resolve()
    index_path = Path(index_dir).resolve()
    state: dict[str, tuple[int, int]] = {}
    for relpath, file_path in _iter_files(root_path, index_path):
        stat_result = file_path.stat()
        state[relpath] = (stat_result.st_mtime_ns, stat_result.st_size)
    return state


def detect_state_changes(
    previous_state: dict[str, tuple[int, int]],
    current_state: dict[str, tuple[int, int]],
) -> set[str]:
    changed: set[str] = set()
    all_paths = set(previous_state) | set(current_state)
    for relpath in all_paths:
        if previous_state.get(relpath) != current_state.get(relpath):
            changed.add(relpath)
    return changed


def _normalize_changed_paths(changed_paths: set[str] | None) -> set[str] | None:
    if changed_paths is None:
        return None
    normalized = {
        path.replace("\\", "/").strip().strip("/")
        for path in changed_paths
        if path.strip()
    }
    return normalized


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = int(round((len(ordered) - 1) * quantile))
    return ordered[position]


def _iter_files(root: Path, index_path: Path) -> Iterable[tuple[str, Path]]:
    try:
        index_relative = index_path.relative_to(root).as_posix()
    except ValueError:
        index_relative = None

    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        relpath = file_path.relative_to(root).as_posix()
        if _is_ignored_relpath(relpath, index_relative):
            continue
        yield relpath, file_path


def _is_ignored_relpath(relpath: str, index_relative: str | None) -> bool:
    if relpath.startswith(".git/"):
        return True
    if index_relative is None:
        return False
    return relpath == index_relative or relpath.startswith(index_relative.rstrip("/") + "/")


def _read_text_file(path: Path) -> str | None:
    with path.open("rb") as handle:
        sample = handle.read(8192)
        if b"\x00" in sample:
            return None
        handle.seek(0)
        data = handle.read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _load_previous_state(index_path: Path) -> tuple[dict[str, dict[str, int]], object | None, object | None]:
    metadata_path = index_path / "metadata.json"
    terms_path = index_path / "doc_terms.bin"
    terms_ci_path = index_path / "doc_terms_ci.bin"
    if not metadata_path.exists() or not terms_path.exists() or not terms_ci_path.exists():
        return {}, None, None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("version") != INDEX_VERSION:
        return {}, None, None
    documents = {
        document["relpath"]: document
        for document in metadata["documents"]
    }
    return documents, open(terms_path, "rb"), open(terms_ci_path, "rb")


def _write_doc_terms(
    index_path: Path,
    doc_records: list[DocumentRecord],
    all_terms: list[list[int]],
    filename: str,
    offset_field: str,
) -> list[DocumentRecord]:
    output = bytearray()
    updated_records: list[DocumentRecord] = []
    for record, terms in zip(doc_records, all_terms, strict=True):
        offset = len(output)
        for term in terms:
            output.extend(term.to_bytes(8, "little"))
        values = asdict(record)
        values[offset_field] = offset
        updated_records.append(DocumentRecord(**values))
    _atomic_write_bytes(index_path / filename, bytes(output))
    return updated_records


def _build_postings(all_terms: list[list[int]]) -> dict[int, list[int]]:
    postings: dict[int, list[int]] = {}
    for doc_id, terms in enumerate(all_terms):
        for term in terms:
            postings.setdefault(term, []).append(doc_id)
    return postings


def _write_postings_and_lookup(index_path: Path, postings: dict[int, list[int]], postings_name: str, lookup_name: str) -> None:
    postings_bytes = bytearray()
    lookup_bytes = bytearray()
    for token_hash in sorted(postings):
        doc_ids = postings[token_hash]
        offset = len(postings_bytes)
        encoded = encode_postings(doc_ids)
        postings_bytes.extend(encoded)
        lookup_bytes.extend(LOOKUP_STRUCT.pack(token_hash, offset, len(encoded), len(doc_ids)))
    _atomic_write_bytes(index_path / postings_name, bytes(postings_bytes))
    _atomic_write_bytes(index_path / lookup_name, bytes(lookup_bytes))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_name = handle.name
    os.replace(temp_name, path)
