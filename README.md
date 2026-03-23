# igrep

`igrep` is a fast local regex prefilter for large codebases.

It builds a trigram index once, then narrows candidate files before running full regex verification. The design follows the same core ideas in Cursor's fast regex write up:

- postings file on disk
- separate sorted lookup table `(hash, offset, length, docfreq)`
- mmap lookup table in the query process
- binary search lookup by hash
- final regex verification for correctness

## Why use it

`ripgrep` is great, but full scans can stall agent loops in large repos.

`igrep` targets repeated searches where a local index can prune files quickly. For agent workflows, that can cut grep latency from seconds to milliseconds on selective patterns.

## Install

### Option 1: pipx (macOS, Linux, Windows)

```bash
pipx install igrep
```

### Option 2: uv tool

```bash
uv tool install igrep
```

### Option 3: from source

```bash
git clone https://github.com/christianwilkins/igrep
cd igrep
python -m pip install .
```

## Commands

```bash
igrep build --root <path> --index-dir <path> [--incremental]
igrep search --pattern <regex> --index-dir <path> [--root <path>] [--ignore-case] [--max-files N] [--files-only] [--line-numbers]
igrep bench --root <path> --index-dir <path>
```

### Typical workflow

```bash
igrep build --root . --index-dir .igrep
igrep search --pattern "build_index|Searcher" --index-dir .igrep --line-numbers
```

## Index format

`igrep build` writes:

- `metadata.json`: index version, root, per doc metadata
- `doc_terms.bin`: per doc trigram hash arrays
- `doc_terms_ci.bin`: case folded trigram hash arrays
- `postings.bin`: delta varint posting lists
- `lookup.bin`: sorted fixed width lookup records
- `postings_ci.bin`, `lookup_ci.bin`: case folded versions

Lookup record layout is 24 bytes little endian:

```text
uint64 hash
uint64 postings_offset
uint32 postings_length
uint32 docfreq
```

## Search path

`igrep search`:

1. extracts required literals from regex
2. decomposes literals to trigrams
3. loads posting metadata by mmap binary search
4. intersects posting lists in ascending `docfreq`
5. verifies candidates with Python regex
6. prints rg style lines or files only mode

If no safe trigram can be extracted, it falls back to scanning indexed files and prints a warning.

## Benchmarks

Run:

```bash
igrep bench --root . --index-dir .igrep
```

The benchmark reports p50 and p95 latency for `igrep` vs `rg` over realistic regex patterns. Results depend on corpus size, cache warmth, and pattern selectivity.

## Limitations

- UTF-8 text only
- files over 2 MB are skipped
- regex literal extraction is conservative
- fallback scans can still happen for very dynamic patterns

## Development

Run tests:

```bash
python -m unittest discover -s tests -v
```

Build index in place:

```bash
python -m igrep build --root . --index-dir .igrep --incremental
```

## License

MIT
