import socket
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: socket.socket, address: object) -> None:
        raise AssertionError(f"test attempted a real network connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
