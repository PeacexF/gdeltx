# gdeltx

A CLI for OSINT investigation using the GDELT APIs.

> **Status: pre-alpha.** The scaffolding is in place; no investigation commands are implemented yet. See `.github/notes/ROADMAP.md` for the build order.

GDELT publishes an enormous stream of global news metadata across several APIs and bulk datasets, each with its own query surface, formats and quirks. `gdeltx` puts one consistent command-line interface over them, normalizes the results, and prints them for a human or pipes them into `jq`, `duckdb` or `pandas`.

## Requirements

* Python 3.14+
* [uv](https://docs.astral.sh/uv/)

## Install

```bash
git clone https://github.com/PeacexF/gdeltx
cd gdeltx
uv sync
uv run gdeltx --help
```

## Planned commands

| Command | Purpose |
|---|---|
| `search` | Article search over the DOC API |
| `context` | How a term appears in article text |
| `entities` | People, organizations, locations and themes |
| `events` | Structured CAMEO event records |
| `sources` | Coverage aggregated by domain |
| `timeline` | Activity bucketed over time |
| `related` | Entities frequently co-occurring with a query |
| `geo` | Geographic distribution of coverage |

Every command will support `table` (default), `json`, `jsonl` and `csv` output.

## Development

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run ruff format .
uv run pytest
```

## License

See [LICENSE](LICENSE).
