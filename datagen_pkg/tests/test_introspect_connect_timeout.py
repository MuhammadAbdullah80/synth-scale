"""Regression test: introspect_db's connect_timeout must reach psycopg2 as
an integer -- libpq rejects a float outright ("invalid integer value '5.0'
for connection option 'connect_timeout'"). Caught by the web playground's
live-Postgres test (float from `float(os.environ[...])` flowed straight
through); this unit test pins it down without needing a real database.
"""
from __future__ import annotations

import datagen.introspect as introspect_mod


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        class _Result:
            def __iter__(self):
                return iter([])

            def mappings(self):
                return self

            def all(self):
                return []

        return _Result()


class _FakeEngine:
    def connect(self):
        return _FakeConnection()

    def dispose(self):
        pass


def test_connect_timeout_reaches_create_engine_as_int(monkeypatch):
    captured = {}

    def fake_create_engine(url, connect_args=None, **kw):
        captured["connect_args"] = connect_args
        return _FakeEngine()

    monkeypatch.setattr(introspect_mod, "create_engine", fake_create_engine)

    try:
        introspect_mod.introspect_db("postgresql://u@h/db", connect_timeout=5.0)
    except ValueError:
        pass  # "no tables found" -- expected with the fake empty engine above

    assert captured["connect_args"] == {"connect_timeout": 5}
    assert isinstance(captured["connect_args"]["connect_timeout"], int)


def test_sub_second_timeout_rounds_up_not_down_to_zero(monkeypatch):
    captured = {}

    def fake_create_engine(url, connect_args=None, **kw):
        captured["connect_args"] = connect_args
        return _FakeEngine()

    monkeypatch.setattr(introspect_mod, "create_engine", fake_create_engine)

    try:
        introspect_mod.introspect_db("postgresql://u@h/db", connect_timeout=0.3)
    except ValueError:
        pass

    # 0 would mean "no timeout" to libpq -- must never truncate down to it.
    assert captured["connect_args"]["connect_timeout"] == 1


def test_no_connect_timeout_means_no_connect_args(monkeypatch):
    captured = {}

    def fake_create_engine(url, connect_args=None, **kw):
        captured["connect_args"] = connect_args
        return _FakeEngine()

    monkeypatch.setattr(introspect_mod, "create_engine", fake_create_engine)

    try:
        introspect_mod.introspect_db("postgresql://u@h/db")
    except ValueError:
        pass

    assert captured["connect_args"] == {}
