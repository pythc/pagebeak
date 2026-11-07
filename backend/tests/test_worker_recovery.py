from types import SimpleNamespace

import pytest
from akb import worker
from sqlalchemy.exc import OperationalError, ProgrammingError


def test_database_outage_retries_and_resets_backoff(monkeypatch):
    outage = OperationalError("", {}, Exception("unavailable"))
    events = iter([outage, outage, False, outage, KeyboardInterrupt()])
    sleeps = []

    def run_once():
        event = next(events)
        if isinstance(event, BaseException):
            raise event
        return event

    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    with pytest.raises(KeyboardInterrupt):
        worker.run_worker(SimpleNamespace(run_once=run_once))
    assert sleeps == [1, 2, 0.5, 1]


def test_programming_errors_are_not_hidden_as_outages(monkeypatch):
    def run_once():
        raise ProgrammingError("", {}, Exception("invalid query"))

    monkeypatch.setattr(worker.time, "sleep", lambda _: pytest.fail("Retried a programming error"))
    with pytest.raises(ProgrammingError):
        worker.run_worker(SimpleNamespace(run_once=run_once))
