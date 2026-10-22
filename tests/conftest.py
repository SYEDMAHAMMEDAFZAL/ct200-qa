from pathlib import Path

import pytest

from app.db import make_engine, make_sessionmaker

DATA = Path(__file__).resolve().parent.parent / "data"
V1 = DATA / "ct200_manual.pdf"
V2 = DATA / "ct200_manual_v2.pdf"


@pytest.fixture
def db():
    session = make_sessionmaker(make_engine("sqlite://"))()
    yield session
    session.close()
