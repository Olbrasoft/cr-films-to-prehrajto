import pytest

from cr_films_to_prehrajto.catalog import (
    CATALOG_SQL,
    ReadOnlyViolation,
    assert_read_only,
    connect_read_only,
)


class Cursor:
    def __init__(self, value):
        self.value = value
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, command):
        self.commands.append(command)

    def fetchone(self):
        return (self.value,)


class Connection:
    def __init__(self, value):
        self.cursor_value = Cursor(value)
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


def test_read_only_guard_accepts_on():
    connection = Connection("on")
    assert_read_only(connection)
    assert connection.cursor_value.commands == ["SHOW transaction_read_only"]


def test_read_only_guard_rejects_writable_and_closes():
    connection = Connection("off")
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(connection)
    assert connection.closed


def test_connection_forces_server_read_only(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "cr_films_to_prehrajto.catalog.psycopg2.connect",
        lambda dsn, **kw: called.update(dsn=dsn, **kw),
    )
    connect_read_only("postgres://db")
    assert called["options"] == "-c default_transaction_read_only=on"


def test_catalog_query_is_film_only_and_uses_listing_predicate():
    assert "JOIN video_sources vs ON vs.film_id = f.id AND vs.is_alive" in CATALOG_SQL
    assert "episode_id" not in CATALOG_SQL
    assert "UPDATE " not in CATALOG_SQL.upper()
    assert "INSERT " not in CATALOG_SQL.upper()
