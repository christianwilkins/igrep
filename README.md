# igrep

`igrep` is an indexed regex search tool for repeated code search.

It builds a trigram index, uses that index to cut candidate files, then runs full regex verification to keep results correct.

## Why use this instead of plain rg

`rg` is excellent for one shot scans.

`igrep` is built for repeated searches in the same repo, especially in agent loops. Once an index exists, selective queries are often much faster than full scans.

## Install

### Linux and macOS binary install

```bash
curl -fsSL https://raw.githubusercontent.com/christianwilkins/igrep/main/scripts/install.sh | bash
```

### Windows PowerShell install

```powershell
iwr https://raw.githubusercontent.com/christianwilkins/igrep/main/scripts/install.ps1 -OutFile install-igrep.ps1
powershell -ExecutionPolicy Bypass -File .\install-igrep.ps1
```

### pipx from GitHub

```bash
pipx install git+https://github.com/christianwilkins/igrep
```

## Core commands

```bash
igrep build --root <path> --index-dir <path> [--incremental]
igrep search --pattern <regex> --index-dir <path> [--root <path>] [--ignore-case] [--max-files N] [--files-only] [--line-numbers] [--jobs N] [--auto-build] [--json]
igrep rg [PATTERN] [PATH] [-e PATTERN] [-i] [-n] [-l] [-m N] [--index-dir .igrep] [--auto-build] [--jobs N] [--json]
igrep bench --root <path> --index-dir <path>
```

## Fast start

```bash
# one time
igrep build --root . --index-dir .igrep

# repeated searches
igrep rg "build_index|bench_index" . --index-dir .igrep -n

# auto refresh index then search
igrep rg "TODO|FIXME" . --index-dir .igrep --auto-build -n
```

## Replace rg in an agent shell

```bash
alias rg='igrep rg --index-dir .igrep --auto-build'
```

This alias supports common rg flags like `-e`, `-i`, `-n`, `-l`, and `-m`.

## Output modes

- default: `path:line` style output
- `--line-numbers`: `path:line_number:line`
- `--files-only`: only matching file paths
- `--json`: structured output for tools and agents

## Index design

`igrep build` writes:

- `metadata.json`: version, root, per file metadata
- `doc_terms.bin`: per file trigram hash arrays
- `doc_terms_ci.bin`: case folded trigram hash arrays
- `postings.bin`: delta varint posting lists
- `lookup.bin`: sorted lookup entries `(hash, offset, length, docfreq)`
- `postings_ci.bin`, `lookup_ci.bin`: case folded index files

Lookup entry layout is 24 bytes little endian:

```text
uint64 hash
uint64 postings_offset
uint32 postings_length
uint32 docfreq
```

## Search flow

`igrep search`:

1. extracts required literals from regex
2. decomposes those literals to trigrams
3. looks up posting metadata through mmap binary search
4. intersects posting lists by ascending doc frequency
5. verifies candidate files with full regex
6. renders line or file output

If there is no usable literal plan, it falls back to indexed file scan and prints a warning.

## Development

```bash
python -m unittest discover -s tests -v
python -m igrep build --root . --index-dir .igrep --incremental
python -m igrep bench --root . --index-dir .igrep
```

## Limitations

- UTF-8 text files only
- files over 2 MB are skipped
- regex literal extraction is conservative
- fallback scans still happen for highly dynamic regex

## License

MIT
