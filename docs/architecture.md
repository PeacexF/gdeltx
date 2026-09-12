# Architecture

> Living document. Expanded as phases land; see `.github/notes/ROADMAP.md`.

## Layers

```text
CLI  →  Commands  →  Sources  →  GDELT
                  ↘  Analysis
                  ↘  Output
```

**CLI** (`cli.py`) parses arguments and owns the shared option group. It holds no investigation logic.

**Commands** (`commands/`) orchestrate: resolve a time range, call one or more sources, hand records to analysis or straight to output.

**Sources** (`sources/`) talk to GDELT and nothing else. HTTP concerns — retries, backoff, rate limiting, caching — live in `sources/http.py` and are shared by every client. Business logic does not belong here.

**Parsers** (`parsers/`) turn raw responses and CSV rows into models. They are pure functions over bytes, which makes them the cheapest layer to test.

**Analysis** (`analysis/`) performs deterministic aggregation and scoring over normalized models. No network, no I/O.

**Output** (`output/`) renders a result envelope as a table, JSON, JSONL or CSV. Data goes to stdout; everything else goes to stderr.

## Two kinds of source

GDELT is not one uniform API, and the package shape reflects that.

**Query APIs** — DOC, Context and GEO answer HTTP requests and return JSON. They cover a rolling recent window and cap how many records a single request may return.

**Bulk files** — GKG and Events have no query endpoint. They are published as tab-delimited CSV files inside zip archives, one set every 15 minutes. Using them means resolving a time range to a file list, downloading, unzipping, parsing and filtering locally.

`sources/files/` exists for the second case. It is the most expensive part of the system: a single day is 96 files per dataset, so caching is mandatory rather than an optimization, and commands that read bulk data guard against unbounded ranges before fetching anything.

## Normalization

Models are normalized for usability but retain a `raw` field carrying the original record, so an export can be traced back to what GDELT actually returned. Models stay permissive — upstream columns shift, and a schema change should degrade a row, not abort a run.

## Reproducibility

Machine-readable output carries the query, endpoint, retrieval timestamp and parameters alongside the results, so any exported dataset can be regenerated later.
