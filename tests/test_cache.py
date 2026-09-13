import time
from pathlib import Path

import pytest

from gdeltx.cache import CacheStore
from gdeltx.cache.store import cache_key
from gdeltx.errors import CacheError


@pytest.fixture
def store(tmp_path: Path) -> CacheStore:
    return CacheStore(tmp_path / "cache", ttl=3600)


def test_key_is_order_independent() -> None:
    assert cache_key("doc", {"a": 1, "b": 2}) == cache_key("doc", {"b": 2, "a": 1})


def test_key_varies_with_endpoint_and_params() -> None:
    assert cache_key("doc", {"q": "x"}) != cache_key("geo", {"q": "x"})
    assert cache_key("doc", {"q": "x"}) != cache_key("doc", {"q": "y"})


def test_key_is_a_safe_path_component() -> None:
    key = cache_key("doc", {"q": "../../etc/passwd"})
    assert key.isalnum()
    assert "/" not in key


def test_miss_then_hit(store: CacheStore) -> None:
    key = cache_key("doc", {"q": "OpenAI"})
    assert store.get(key) is None
    store.set(key, b"payload", metadata={"url": "https://example"})
    entry = store.get(key)
    assert entry is not None
    assert entry.body == b"payload"
    assert entry.metadata["url"] == "https://example"


def test_binary_survives_round_trip(store: CacheStore) -> None:
    blob = bytes(range(256))
    store.set("deadbeef", blob)
    entry = store.get("deadbeef")
    assert entry is not None
    assert entry.body == blob


def test_expiry(tmp_path: Path) -> None:
    store = CacheStore(tmp_path, ttl=0)
    store.set("k", b"v")
    time.sleep(0.01)
    assert store.get("k") is None


def test_ttl_none_never_expires(tmp_path: Path) -> None:
    CacheStore(tmp_path, ttl=0).set("k", b"v")
    assert CacheStore(tmp_path, ttl=None).get("k") is not None


def test_disabled_store_reads_and_writes_nothing(tmp_path: Path) -> None:
    store = CacheStore(tmp_path, enabled=False)
    store.set("k", b"v")
    assert store.get("k") is None
    assert not (tmp_path / "k").exists()


def test_corrupt_metadata_is_discarded(store: CacheStore) -> None:
    store.set("abcdef", b"v")
    meta_path = next(store.directory.rglob("*.json"))
    meta_path.write_text("not json", encoding="utf-8")
    assert store.get("abcdef") is None
    assert not meta_path.exists()


def test_missing_body_is_a_miss(store: CacheStore) -> None:
    store.set("abcdef", b"v")
    next(store.directory.rglob("*.body")).unlink()
    assert store.get("abcdef") is None


def test_unwritable_directory_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(CacheError):
        CacheStore(blocker).set("k", b"v")


def test_eviction_respects_limit(tmp_path: Path) -> None:
    store = CacheStore(tmp_path, max_bytes=300)
    for i in range(10):
        store.set(f"{i:02d}aaaa", b"x" * 100)
    assert store.evict_to_limit() > 0
    assert store.total_bytes() <= 300


def test_clear(store: CacheStore) -> None:
    store.set("k", b"v")
    store.clear()
    assert not store.directory.exists()
