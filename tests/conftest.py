import socket

import pytest

from theo.config import Settings
from theo.storage import Database


class FakeClock:
    def __init__(self):
        self.value = 1788782400.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("Tests must not use network/model accounts")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setenv("THEO_TEST_OFFLINE", "1")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings():
    # Synthetic test storage: this is not evidence that a production disk is encrypted.
    return Settings(encrypted_storage_verified=True)


@pytest.fixture
async def db(tmp_path, clock):
    database = Database(tmp_path / "data", clock)
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def conversation(db):
    return await db.conversation("owner", "local", "owner")
