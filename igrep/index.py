from __future__ import annotations

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


def build_index(root: str, index_dir: str, incremental: bool = False) -> BuildStats:
    root_path = Path(root).resolve()
    index_path = Path(index_dir).resolve()
    index_path.mkdir(parents=True, exist_ok=True)

    previous_docs: dict[str, dict[str, int]] = {}
    old_terms = None
    old_terms_ci = None
    if incremental:
        previous_docs, old_terms, old_terms_ci = _load_previous_state(index_path)

    doc_records: list[DocumentRecord] = []
    doc_terms: list[list[int]] = []
    doc_terms_ci: list[list[int]] = []
    reused_files = 0
    skipped_files = 0
    indexed_files = 0

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
    return BuildStats(indexed_files=indexed_files, reused_files=reused_files, skipped_files=skipped_files, trigram_terms=trigram_terms)


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

    def close(self) -> None:
        self.lookup.close()
        self.lookup_ci.close()
        self.postings.close()
        self.postings_ci.close()

    def search(self, pattern: str, ignore_case: bool = False, max_files: int | None = None) -> SearchOutcome:
        literal_groups = extract_literal_groups(pattern, ignore_case=ignore_case)
        lookup = self.lookup_ci if ignore_case else self.lookup
        postings_handle = self.postings_ci if ignore_case else self.postings

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
            candidate_list = self._load_posting(postings_handle, entries[0])
            for entry in entries[1:]:
                candidate_list = intersect_postings(candidate_list, self._load_posting(postings_handle, entry))
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
        matches: list[str] = []
        verified_files = 0
        for doc_id in candidates:
            relpath = self.documents[doc_id].relpath
            file_path = self.root / relpath
            text = _read_text_file(file_path)
            if text is None:
                continue
            verified_files += 1
            if regex.search(text):
                matches.append(relpath)
                if max_files is not None and len(matches) >= max_files:
                    break

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

    def _load_posting(self, handle, entry) -> list[int]:
        handle.seek(entry.offset)
        data = handle.read(entry.length)
        return decode_postings(data)


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
            # defensive fallback for zero-width or complex constructs
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
        index_relative = index_path.relative_to(root)
    except ValueError:
        index_relative = None
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        relpath = file_path.relative_to(root).as_posix()
        if relpath.startswith(".git/"):
            continue
        if index_relative is not None and relpath == index_relative.as_posix():
            continue
        if index_relative is not None and relpath.startswith(index_relative.as_posix().rstrip("/") + "/"):
            continue
        yield relpath, file_path


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
