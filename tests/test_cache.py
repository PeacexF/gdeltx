import time
from pathlib import Path

import pytest

from gdeltx.cache import CacheStore
from gdeltx.cache.store import cache_key
from gdeltx.errors import CacheError

K = cache_key("k")


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
    store.set(K, blob)
    entry = store.get(K)
    assert entry is not None
    assert entry.body == blob


def test_expiry(tmp_path: Path) -> None:
    store = CacheStore(tmp_path, ttl=0)
    store.set(K, b"v")
    time.sleep(0.01)
    assert store.get(K) is None


def test_ttl_none_never_expires(tmp_path: Path) -> None:
    CacheStore(tmp_path, ttl=0).set(K, b"v")
    assert CacheStore(tmp_path, ttl=None).get(K) is not None


def test_disabled_store_reads_and_writes_nothing(tmp_path: Path) -> None:
    store = CacheStore(tmp_path, enabled=False)
    store.set(K, b"v")
    assert store.get(K) is None
    assert not any(tmp_path.iterdir())


def test_corrupt_metadata_is_discarded(store: CacheStore) -> None:
    store.set(K, b"v")
    meta_path = next(store.directory.rglob("*.json"))
    meta_path.write_text("not json", encoding="utf-8")
    assert store.get(K) is None
    assert not meta_path.exists()


def test_missing_body_is_a_miss(store: CacheStore) -> None:
    store.set(K, b"v")
    next(store.directory.rglob("*.body")).unlink()
    assert store.get(K) is None


def test_unwritable_directory_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(CacheError):
        CacheStore(blocker).set(K, b"v")


def test_eviction_respects_limit(tmp_path: Path) -> None:
    store = CacheStore(tmp_path, max_bytes=300)
    for i in range(10):
        store.set(cache_key(str(i)), b"x" * 100)
    assert store.evict_to_limit() > 0
    assert store.total_bytes() <= 300


@pytest.mark.parametrize("key", ["../../etc/passwd", "k", "AB" * 32, "/tmp/" + "a" * 59])
def test_only_digests_are_accepted_as_keys(store: CacheStore, key: str) -> None:
    with pytest.raises(CacheError):
        store.set(key, b"v")
    with pytest.raises(CacheError):
        store.get(key)
    with pytest.raises(CacheError):
        store.discard(key)


def test_eviction_never_touches_files_it_did_not_write(tmp_path: Path) -> None:
    precious = [tmp_path / "notes.body", tmp_path / "ab" / "mine.body", tmp_path / "x" / "y.json"]
    for path in precious:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"x" * 1000)
    store = CacheStore(tmp_path, max_bytes=0)
    store.set(K, b"v")
    assert store.total_bytes() < 1000
    assert store.evict_to_limit() == 1
    assert all(path.exists() for path in precious)
