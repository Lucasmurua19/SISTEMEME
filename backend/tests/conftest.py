from __future__ import annotations

import pytest

from app.infrastructure.db import make_engine, make_session_factory, uow_factory
from app.infrastructure.orm import Base


@pytest.fixture()
def engine():
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture()
def uow(session_factory):
    return uow_factory(session_factory)
