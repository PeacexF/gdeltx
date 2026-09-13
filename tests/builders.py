"""Build GKG and Events rows and zipped bulk files for tests."""

import io
import zipfile
from datetime import UTC, datetime

import httpx
import respx

from gdeltx.parsers.events import EVENT_COLUMNS
from gdeltx.parsers.gkg import GKG_COLUMNS
from gdeltx.sources.files.index import LASTUPDATE_URL, SLOT, Dataset, floor_slot, slot_url


def gkg_line(**values: str) -> str:
    fields = dict.fromkeys(GKG_COLUMNS, "")
    fields.update(values)
    return "\t".join(fields[name] for name in GKG_COLUMNS) + "\n"


def event_line(**values: str) -> str:
    fields = dict.fromkeys(EVENT_COLUMNS, "")
    fields.update(values)
    return "\t".join(fields[name] for name in EVENT_COLUMNS) + "\n"


def zipped(name: str, lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, "".join(lines))
    return buffer.getvalue()


def mock_published(
    dataset: Dataset, files: dict[int, list[str]], *, now: datetime | None = None
) -> dict[int, respx.Route]:
    """Mock lastupdate.txt at the current slot, and file ``i`` as the i-th slot back from it.

    Slots without an entry answer 404, which the file layer treats as a gap.
    """
    latest = floor_slot(now or datetime.now(UTC))
    stamp_text = f"{latest:%Y%m%d%H%M%S}"
    respx.get(LASTUPDATE_URL).mock(
        return_value=httpx.Response(
            200, text=f"1 x http://data.gdeltproject.org/gdeltv2/{stamp_text}.{dataset.suffix}\n"
        )
    )
    routes = {}
    for back, lines in files.items():
        stamp = latest - SLOT * back
        routes[back] = respx.get(slot_url(dataset, stamp)).mock(
            return_value=httpx.Response(200, content=zipped(f"{stamp:%Y%m%d%H%M%S}.csv", lines))
        )
    # respx takes the first matching route, so the catch-all must come last.
    respx.get(url__startswith="https://data.gdeltproject.org/gdeltv2/2").mock(
        return_value=httpx.Response(404)
    )
    return routes
