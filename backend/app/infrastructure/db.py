"""Motor, sesiones y unidad de trabajo."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.infrastructure import repositories as repo


def make_engine(url: str, *, echo: bool = False) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url.rstrip("/") in ("sqlite://", "sqlite:"):
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, echo=echo, **kwargs)

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _record):  # pragma: no cover - configuración de SQLite para pruebas
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        return engine
    return create_engine(url, echo=echo, pool_pre_ping=True, pool_size=10, max_overflow=10, pool_recycle=1800)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


class SqlUnitOfWork:
    """``with factory() as uow:`` abre una transacción; ``commit`` al salir sin error."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> SqlUnitOfWork:
        s = self._factory()
        self.session = s
        self.audit = repo.AuditRepo(s)
        self.checkpoints = repo.CheckpointRepo(s)
        self.chain = repo.ChainRepo(s)
        self.snapshots = repo.SnapshotRepo(s)
        self.quotes = repo.QuoteRepo(s)
        self.strategies = repo.StrategyRepo(s, self.audit)
        self.evaluations = repo.EvaluationRepo(s)
        self.signals = repo.SignalRepo(s)
        self.sessions = repo.SessionRepo(s, self.audit)
        self.positions = repo.PositionRepo(s)
        self.attempts = repo.AttemptRepo(s)
        self.ledger = repo.LedgerRepo(s)
        self.jobs = repo.JobRepo(s)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        assert self.session is not None
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()


def uow_factory(session_factory: sessionmaker[Session]):
    def make() -> SqlUnitOfWork:
        return SqlUnitOfWork(session_factory)

    return make
