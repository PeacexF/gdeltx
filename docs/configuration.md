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

[cache]
enabled = true
ttl = 3600

[output]
format = "table"
```

## Keys

| Key | Default | Meaning |
|---|---|---|
| `api.timeout` | `30` | Per-request timeout in seconds |
| `api.retries` | `3` | Retry attempts before giving up |
| `cache.enabled` | `true` | Whether responses are cached |
| `cache.ttl` | `3600` | Cache lifetime in seconds |
| `output.format` | `"table"` | One of `table`, `json`, `jsonl`, `csv` |

## Cache

Cached responses live in `~/.cache/gdeltx/`, keyed by a hash of the endpoint and its normalized parameters. Each entry stores the request metadata, the response body and a timestamp.

Bulk GDELT files are immutable once published, so they are cached by name and do not expire; they are evicted by size rather than age.

A cached result is never presented as fresh — `--verbose` reports cache hits on stderr.

Overrides:

```bash
gdeltx search "..." --no-cache
gdeltx search "..." --cache-ttl 600
```

## Environment variables

Not supported yet.
