# Configuration

> Living document. Options land as their phases do; see `.github/notes/ROADMAP.md`.

## Precedence

```text
CLI flags  >  config file  >  built-in defaults
```

A missing config file is not an error.

## Location

```text
~/.config/gdeltx/config.toml
```

## Example

```toml
[api]
timeout = 30
retries = 3
min_interval = 5.0

[cache]
enabled = true
ttl = 3600

[files]
warn_files = 192
max_files = 1000
workers = 4

[output]
format = "table"
```

## Keys

| Key | Default | Meaning |
|---|---|---|
| `api.timeout` | `30` | Per-request timeout in seconds |
| `api.retries` | `3` | Retry attempts before giving up |
| `api.min_interval` | `5.0` | Minimum seconds between API requests |
| `api.user_agent` | unset | Override the User-Agent sent to GDELT |
| `cache.enabled` | `true` | Whether responses are cached |
| `cache.ttl` | `3600` | Cache lifetime in seconds |
| `files.warn_files` | `192` | Warn before downloading more bulk files than this |
| `files.max_files` | `1000` | Refuse larger ranges unless `--allow-large` is passed |
| `files.workers` | `4` | Concurrent bulk file downloads |
| `output.format` | `"table"` | One of `table`, `json`, `jsonl`, `csv` |

## Cache

API responses live in `~/.cache/gdeltx/api/`, keyed by a hash of the endpoint and its normalized parameters. Each entry stores the request metadata, the response body and a timestamp.

Bulk GDELT files live in `~/.cache/gdeltx/files/`. They are immutable once published, so they never expire; when the store exceeds `cache.max_bytes`, the least recently used files are evicted.

A cached result is never presented as fresh — `--verbose` reports cache hits on stderr.

Overrides:

```bash
gdeltx search "..." --no-cache
gdeltx search "..." --cache-ttl 600
```

## User-Agent

GDELT answers `429` both when rate limiting and when it refuses a client's User-Agent, so a
429 on the very first request usually means the User-Agent, not the request rate.

By default gdeltx identifies itself honestly:

```text
gdeltx/<version> (+https://github.com/PeacexF/gdeltx)
```

If GDELT refuses that, override it:

```toml
[api]
user_agent = "..."
```

## Environment variables

Not supported yet.
