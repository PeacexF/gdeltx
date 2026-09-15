# Architecture

How gdeltx is put together, and why. For decisions and their history, see `.github/notes/ROADMAP.md` (the deltas table) and `.github/notes/API-NOTES.md` (what GDELT actually does, with evidence).

## Layers

```text
cli.py ──► commands/ ──► sources/ ──────► GDELT
                │           │
                │           └─► parsers/ ──► models/
                ├─► analysis/
                └─► output/  ──► stdout
console.py (Reporter) ────────────────────► stderr
```

| Layer | Owns | Must not |
|---|---|---|
| `cli.py` | Arguments, the shared option set, config overrides, the top-level error handler (`main`) | Contain investigation logic |
| `commands/` | One module per command: resolve the range, call sources, hand records to analysis and output | Talk HTTP or parse raw rows |
| `sources/` | Reaching GDELT: `http.py` (retries, backoff, rate limit, cache, host allowlist), `doc.py`, `context.py`, `files/` | Aggregate or format |
| `parsers/` | Pure functions from JSON payloads and TSV rows to models; CAMEO labels | Do I/O |
| `models/` | Pydantic records plus `RequestMeta`, the provenance carried into machine output | Validate strictly; see below |
| `analysis/` | Deterministic counting (`aggregate.py`) and co-occurrence scoring (`cooccurrence.py`) | Do I/O |
| `output/` | Table, JSON, JSONL and CSV writers | Write anywhere but the given stream |
| `cache/` | The on-disk store | Accept a key that is not a SHA-256 digest |
| `console.py` | `Reporter`: warnings, debug, progress and errors, all on stderr | Touch stdout |

## Two kinds of source

GDELT is not one uniform API, and the package reflects that.

### Query APIs: `search`, `context`, and the article half of `timeline` and `sources`

- **Endpoints:** DOC 2.0 and Context 2.0 answer HTTP GETs with JSON.
- **Throttling:** GDELT throttles to about one request every 5 seconds, so every API request goes through one `RateLimiter`.
- **Retries:** timeouts, connection errors, 429 and 5xx are retried with capped exponential backoff and jitter. Anything else fails immediately.
- **Errors in 200s:** GDELT reports some errors as HTTP 200 with a short plain-text body. `Fetched.json()` turns those into `APIError` rather than a parse failure or, worse, an empty result.
- **Paging:** DOC has no offset and a 250-record cap. `doc.search` pages with a time cursor: each request ends where the previous page's oldest article was, overlapping by a second and deduplicating by URL.

### Bulk files: `entities`, `events`, `related`, `geo`, and the GKG/event halves of `sources` and `timeline`

GKG and Events have no query API. They are zipped, headerless TSVs published every 15 minutes. `sources/files/` handles them in these steps:

1. **`index.py`** maps a time range to 15-minute stamps and derives each URL. It fetches only `lastupdate.txt`, uncached, to learn the newest published stamp. It never downloads the tens-of-MB master list. A 404 is a gap, not an error.
2. **`fetch.guard`** estimates the file count before anything is downloaded. It warns above `files.warn_files` and refuses above `files.max_files` unless `--allow-large` is passed.
3. **`FileFetcher.iter_files`**:
   - downloads on a small thread pool, but hands files out strictly in time order, and holds only a bounded window of downloads at once;
   - decompresses each archive lazily while its lines are consumed;
   - on a corrupt archive, warns, evicts it from the cache, and keeps going;
   - fails the run only if every file fails.
4. **`readers.py`** pre-filters raw lines by a case-insensitive substring before parsing, so non-matching rows are never split into columns. It then checks that the match falls in a meaningful field (names or page title for GKG, actor names for Events) and not in a URL.

Memory is flat in the size of the range, and `tests/test_hardening.py` measures that rather than assuming it. The remaining per-run state is bounded by what matches: aggregation tallies, the set of event IDs already seen, and, for `related`, the candidate entities.

`related` reads the same files twice. The first pass collects the articles naming the query. The second counts how common each candidate entity is across all articles, which is what its score normalizes by. The files are cached by the first pass, so the second is local.

## Models and provenance

- **Permissive records:** `Record` models accept unknown fields (`extra="allow"`) because GDELT changes field sets without notice. An unexpected column should widen a record, not abort a run.
- **Raw rows:** each record can carry `raw`, the original row, exported with `--raw`.
- **Provenance:** `RequestMeta` travels alongside every result set: query, endpoint, retrieval time, the exact parameters sent (or the file range read) and whether the cache answered. JSON puts it at the top of the document, GeoJSON in a `gdeltx` member, and JSONL leaves it out so the stream stays records only.

## Output and the process boundary

- **stdout** carries data only. Every diagnostic goes through `Reporter` to stderr, and progress bars are hidden unless stderr is a terminal.
- **Streaming:** JSONL and CSV consume their input lazily. JSON cannot stream, by the nature of the format.
- **Errors:** `main()` is the only place exceptions become exit codes. `GdeltxError` subclasses map to sysexits-style codes (`64` input, `65` parse, `70` API, `74` cache, `75` rate limited, `78` config) and render as one `ERROR:` line plus an optional hint, never a traceback.
- **Broken pipes:** stdout is flushed inside `main()`, so a reader that exits early (`| head`) ends the run quietly with exit `0` instead of a shutdown-time `BrokenPipeError`.
- **Module entry point:** `python -m gdeltx` goes through the same `main()`.

## Determinism

Identical input gives byte-identical output.

- **Ranking:** every ordering has a full tie-break (count, then case-folded name, then name), never dict or set order.
- **Floats:** averages and scores are rounded before sorting, so float summation order cannot reorder results.
- **Spelling:** names differing only in case or spacing are merged and shown in their most common spelling. Nothing fuzzier is merged, except that locations use GDELT's own geocoder identity (ROADMAP D25).

## Security

- **No shell:** no module imports `subprocess` or calls `os.system`, `eval` or `exec`, and a test enforces it.
- **Hosts:** HTTP goes only to `api.gdeltproject.org` and `data.gdeltproject.org` over HTTPS. The check runs as a request hook, so it also covers every redirect hop.
- **Cache paths:** cache keys must be SHA-256 digests, so user input never reaches the filesystem. Eviction and size accounting only consider files with that naming.
- **Credentials:** GDELT needs none, and none are stored.
- **Filesystem:** the only files gdeltx writes are its own cache entries; nothing else is written or deleted.

## Testing

- **No network:** a `conftest.py` fixture makes any real socket connection fail the test, and HTTP is mocked with `respx`.
- **Fixtures:** GDELT data comes from `tests/fixtures/`, a mix of captured responses and rows, and synthetic ones where capture failed (named `*.synthetic.*`).
- **End to end:** `tests/test_workflow.py` runs the whole investigation through `main()` and audits every command for stdout/stderr separation, exit codes, `--quiet`, `--verbose` and `--no-cache`.
- **Process-level:** `tests/test_hardening.py` covers behaviour that needs a real process: broken pipes, `python -m`, and streaming memory.
