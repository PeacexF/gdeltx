# gdeltx

A command-line tool for OSINT investigation over [GDELT](https://www.gdeltproject.org/): news search, quoted context, entities, CAMEO events, sources, timelines, co-occurrence and geography, from one consistent interface.

> **Status: alpha.** Every command below works. Install from source; there is no PyPI release yet.

## Why

GDELT monitors news worldwide and publishes it as several very different things: JSON query APIs with short, uneven coverage windows, and bulk tab-separated files published every 15 minutes with no query engine at all. Each has its own formats, limits and failure modes, and some report errors as HTTP 200 with a plain-text body.

`gdeltx` puts one CLI over all of it. It normalizes results, is honest about coverage limits instead of silently returning less, caches everything, and prints either a readable table or clean JSON, JSONL or CSV for `jq`, DuckDB or pandas.

It does not interpret the data for you. Co-occurrence is labelled as co-occurrence, and a GDELT mention is not a verified fact.

## Install

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/PeacexF/gdeltx
cd gdeltx
uv sync
uv run gdeltx --help
```

## Quick start

```bash
uv run gdeltx search "OpenAI"                      # recent articles
uv run gdeltx entities "OpenAI" --since 6h         # who and what the coverage names
uv run gdeltx search "OpenAI" --since 7d --jsonl > openai.jsonl
```

## Commands

| Command | Source | Coverage | What it answers |
|---|---|---|---|
| `search QUERY` | DOC 2.0 API | last 3 months | Which articles match? |
| `context QUERY` | Context 2.0 API | last 72 hours | What do the articles actually say? |
| `entities TERM` | GKG files | range guard | Which people, organizations, places and themes are named? |
| `events TERM` | Events files | range guard | Which CAMEO events have a matching actor? |
| `sources QUERY` | DOC API or GKG | as above | Which domains cover it, in which country, language and tone? |
| `timeline TERM` | DOC timeline + Events | articles since 2017 | How has activity changed over time? |
| `related TERM` | GKG files | range guard | What co-occurs with it, weighted against how common it is? |
| `geo TERM` | GKG files | range guard | Where does the coverage take place? |

**Two kinds of query.**
- `search` and `context` take GDELT's native query language, passed through unchanged: `'"Company X" (sanctions OR fine) -sport'`.
- The bulk-file commands (`entities`, `events`, `related`, `geo`, `timeline`, and `sources --from gkg`) take one plain name or phrase, matched case-insensitively. Files have no query engine, so boolean syntax is refused rather than matched literally.

**Common options.**

| Option | Meaning |
|---|---|
| `--since`, `--until` | `1h`, `24h`, `7d`, `30d`, `1y`, or a date such as `2026-09-01` |
| `--max N` | Records to return (`search`, `context`, `events`) |
| `--top N` | Rows per category (`entities`, `related`, `sources`, `geo`) |
| `--type`, `--level` | Restrict entity categories or location levels |
| `--allow-large` | Read more bulk files than `files.max_files` allows |
| `--raw` | Keep the original GDELT record in machine output |
| `--no-cache`, `--cache-ttl N` | Bypass the cache, or change its lifetime |
| `-v`/`--verbose`, `-q`/`--quiet` | Progress and cache hits on stderr, or silence |

Every command has `--help`.

## Output formats

```bash
gdeltx search "OpenAI"            # table (default)
gdeltx search "OpenAI" --json     # one document: metadata + results
gdeltx search "OpenAI" --jsonl    # one record per line, streamed
gdeltx search "OpenAI" --csv
gdeltx geo "OpenAI" --geojson     # geo only: a GeoJSON FeatureCollection
```

- Data goes to stdout and nothing else does. Warnings, progress and errors go to stderr, so `--json > out.json` is always valid JSON.
- JSON output records how the data was obtained (`query`, `endpoint`, `retrieved_at`, `parameters`, `cached`), so an export can be reproduced.
- JSONL is records only, with no header line.
- Failures exit non-zero and are never shown as an empty result: `64` for bad input, `70` for an upstream failure, `75` when rate limited, `78` for a bad config.

## A worked investigation

```bash
# 1. Find recent coverage, in GDELT's query language
gdeltx search '"Company X" sanctions' --since 30d

# 2. Read the passages that matched (Context covers the last 72 hours)
gdeltx context '"Company X" sanctions' --since 72h

# 3. Who and what does the coverage name?
gdeltx entities "Company X" --since 24h

# 4. Structured events with Company X as an actor
gdeltx events "Company X" --since 24h

# 5. Which outlets carry the story, and in what tone?
gdeltx sources '"Company X" sanctions' --since 30d
gdeltx sources "Company X" --from gkg --since 24h

# 6. A year of article volume, next to the events the file limit allows
gdeltx timeline "Company X" --since 1y --bars

# 7. What co-occurs with Company X more than chance would suggest?
gdeltx related "Company X" --since 24h

# 8. Where is it happening?
gdeltx geo "Company X" --since 24h --geojson > company-x.geojson

# 9. Export for further analysis
gdeltx search '"Company X"' --since 30d --max 1000 --jsonl > company-x.jsonl
duckdb -c "select domain, count(*) from 'company-x.jsonl' group by 1 order by 2 desc"
```

`related` prints a tree scored by overlap: shared articles divided by the articles naming either the entity or the query. That keeps names that appear everywhere, like `United States`, from topping every list. Every record carries `"relation": "co-occurs with"` and the counts behind its score, so the ranking can be checked by hand.

## Caching

Responses are cached under `~/.cache/gdeltx/` (or `$XDG_CACHE_HOME/gdeltx/`):

- **`api/`**: DOC and Context responses, kept for `cache.ttl` seconds (1 hour by default).
- **`files/`**: GDELT bulk files. They never change once published, so they never expire. The store is capped by `cache.max_bytes` (5 GB by default) and evicts least-recently-used files first.

A repeated file-backed command downloads nothing except the small `lastupdate.txt` index. `--verbose` reports cache hits, and for `search` and `context`, `cached` in JSON output says whether the results were served from the cache. To clear the cache, delete the directory. The cache holds your queries and their results, and nothing else.

Configuration lives in `~/.config/gdeltx/config.toml`; see [docs/configuration.md](docs/configuration.md).

## API limitations

These are GDELT's limits, not gdeltx features. gdeltx warns on stderr whenever it applies one.

- **DOC article search covers about 3 months** and returns at most 250 articles per request, with no paging. gdeltx pages by walking backwards in time, so `--max 1000` works, but a request cannot reach past the window. Longer ranges are clamped, with a warning.
- **DOC rate limits hard.** GDELT asks for at most one request every 5 seconds; gdeltx spaces requests by `api.min_interval` and retries a 429 with backoff. Article lists are refused far more often than timelines.
- **Context covers only the last 72 hours** and at most 200 records.
- **The GKG and Events datasets have no query API.** They are files, 96 a day per dataset, so a day of GKG is roughly 140 MB compressed. gdeltx downloads them in parallel and reads them in a streaming pass. It warns above 192 files and refuses above 1,000 (about 10 days) unless you pass `--allow-large`. `timeline` covers events only for the most recent slice that fits and leaves older event cells blank, not zero.
- **Matching over files is plain text.** A name must appear in a record's person, organization or name fields, or its page title (GKG), or in an actor name (Events).
- **The GEO 2.0 API has been retired.** Every request to it returns 404, so `geo` uses the geocoded locations inside GKG instead.
- **Counts are articles, not mentions.** An article that names someone ten times counts once.

## Development

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Tests never touch the network: HTTP is mocked with `respx`, and GDELT data comes from recorded fixtures in `tests/fixtures/`. `scripts/capture-fixtures.sh` refreshes those from the live service. See [docs/architecture.md](docs/architecture.md) for how the code is organized.

## License

MIT; see [LICENSE](LICENSE).
