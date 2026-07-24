"""Gate 3: list-endpoint contract checks (task E0.3; decision D7).

The pagination utility is exercised against a throwaway model on its own
DeclarativeBase (never app.db.Base, which must stay empty until a migration
exists for it, or the autogenerate-diff gate test would fail) plus a test-only
route for the FastAPI parameter-bound behaviors.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.api.pagination import ListResponse, PageParams, apply_page, parse_sort
from app.errors import AppError


class _ScratchBase(DeclarativeBase):
    pass


class Widget(_ScratchBase):
    __tablename__ = "widget"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    rank: Mapped[int]


SORTABLE = {"name": Widget.name, "rank": Widget.rank}

ROWS = [
    {"id": 1, "name": "alpha", "rank": 3},
    {"id": 2, "name": "bravo", "rank": 1},
    {"id": 3, "name": "bravo", "rank": 2},
    {"id": 4, "name": "delta", "rank": 2},
]


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _ScratchBase.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(Widget(**row) for row in ROWS)
        db.commit()
        yield db
    engine.dispose()


def _names(session: Session, params: PageParams) -> list[str]:
    statement = apply_page(select(Widget), params, SORTABLE)
    return [widget.name for widget in session.scalars(statement)]


def test_limit_and_offset_window_the_results(session: Session):
    assert _names(session, PageParams(limit=2, offset=0, sort="name")) == ["alpha", "bravo"]
    assert _names(session, PageParams(limit=2, offset=2, sort="name")) == ["bravo", "delta"]


def test_sort_ascending_and_descending(session: Session):
    assert _names(session, PageParams(sort="name")) == ["alpha", "bravo", "bravo", "delta"]
    assert _names(session, PageParams(sort="-name")) == ["delta", "bravo", "bravo", "alpha"]


def test_multi_key_sort(session: Session):
    statement = apply_page(select(Widget), PageParams(sort="rank,-name"), SORTABLE)
    ordered = [(w.rank, w.name) for w in session.scalars(statement)]
    assert ordered == [(1, "bravo"), (2, "delta"), (2, "bravo"), (3, "alpha")]


def test_unknown_sort_field_raises_the_envelope_error():
    with pytest.raises(AppError) as excinfo:
        parse_sort("nope", SORTABLE)
    assert excinfo.value.code == "validation_error"
    assert excinfo.value.detail == {"allowed": ["name", "rank"]}


def test_list_response_matches_the_d7_envelope():
    payload = ListResponse[str](items=["a"], total=10, limit=1, offset=0).model_dump()
    assert set(payload) == {"items", "total", "limit", "offset"}


# --- parameter bounds enforce at the model layer ---


def test_default_limit_applies():
    assert PageParams().limit == 50


def test_limit_bounds_enforced():
    with pytest.raises(ValueError):
        PageParams(limit=0)
    with pytest.raises(ValueError):
        PageParams(limit=501)


def test_negative_offset_rejected():
    with pytest.raises(ValueError):
        PageParams(offset=-1)
