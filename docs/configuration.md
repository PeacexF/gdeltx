# Configuration

gdeltx works with no configuration. A config file only changes defaults.

## Precedence

```text
CLI flags  >  config file  >  built-in defaults
```

## Location

```text
$XDG_CONFIG_HOME/gdeltx/config.toml     # usually ~/.config/gdeltx/config.toml
```

A missing file is not an error. `--config PATH` reads a different file, and that file must exist. `gdeltx --verbose ...` prints which config file was loaded.

Unknown sections, unknown keys and wrongly typed values are errors (exit `78`) rather than being silently ignored, so a typo cannot quietly leave a default in place.

## Example

```toml
[api]
timeout = 30
retries = 3
min_interval = 5.0

[cache]
enabled = true
ttl = 3600
max_bytes = 5368709120

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
| `api.timeout` | `30` | Per-request timeout, in seconds |
| `api.retries` | `3` | Retries after a timeout, connection error, 429 or 5xx; other errors are not retried |
| `api.min_interval` | `5.0` | Minimum seconds between requests to `api.gdeltproject.org`; bulk file downloads are not throttled |
| `api.user_agent` | `gdeltx/<version> (+repo URL)` | User-Agent sent to GDELT |
| `cache.enabled` | `true` | Read and write the cache; `--no-cache` turns it off for one run |
| `cache.ttl` | `3600` | Lifetime of API responses, in seconds; `--cache-ttl` overrides it. Bulk files never expire |
| `cache.directory` | `$XDG_CACHE_HOME/gdeltx` | Where the cache lives |
| `cache.max_bytes` | `5368709120` (5 GB) | Size cap for each of the two stores; least recently used entries are evicted first |
| `files.warn_files` | `192` (2 days) | Warn before reading more bulk files than this |
| `files.max_files` | `1000` (~10 days) | Refuse to read more bulk files than this unless `--allow-large` is passed |
| `files.workers` | `4` | Concurrent bulk file downloads |
| `output.format` | `"table"` | Default output: `table`, `json`, `jsonl` or `csv` |

`files.warn_files` must be positive and no greater than `files.max_files`.

## Cache

```text
<cache.directory>/api/     DOC and Context responses, expire after cache.ttl
<cache.directory>/files/   GDELT bulk files, immutable, evicted by size only
```

- **Entries:** each is a pair of files named by a SHA-256 digest of the URL and its normalized parameters: a `.body` with the response bytes and a `.json` sidecar with the request and when it was stored. User input never becomes part of a path.
- **Eviction:** only touches files with that exact naming, so pointing `cache.directory` at a shared folder cannot delete anything gdeltx did not write.
- **Clearing:** delete the directory; nothing else holds state.
- **Privacy:** the `api/` sidecars record the queries you ran. Delete the cache to remove them.

The small `lastupdate.txt` index, which says which bulk files exist, is always fetched fresh.

## Rate limiting and the User-Agent

GDELT asks for no more than one API request every 5 seconds and answers faster clients with `429` and a plain-text explanation. gdeltx spaces requests by `api.min_interval`, retries a 429 with backoff, and shows GDELT's message if every retry is refused.

The DOC article list (`search`, `sources --from doc`) is refused far more readily than timelines or Context, even at 6 seconds' spacing (`.github/notes/API-NOTES.md` §2). If `search` keeps failing with exit `75`, wait a few minutes and raise `api.min_interval`.

An earlier theory that GDELT rejects honest User-Agents was tested and disproved: the default is accepted. `api.user_agent` remains available but should not normally be needed.

## Environment variables

`XDG_CONFIG_HOME` and `XDG_CACHE_HOME` move the config and cache locations. `HTTPS_PROXY` and the other standard proxy variables are honoured. gdeltx has no variables of its own.
