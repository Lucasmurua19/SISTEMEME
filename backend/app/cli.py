"""Utilidades de línea de comandos.

    python -m app.cli migrate              # aplica las migraciones de Alembic (PostgreSQL o SQLite)
    python -m app.cli init-db              # crea las tablas directamente (solo desarrollo con SQLite)
    python -m app.cli replay               # reproduce los escenarios de fixtures sin esperas y resume el resultado
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.logging_config import configure_logging
from app.settings import get_settings


def cmd_migrate() -> int:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")
    print("migraciones aplicadas")
    return 0


def cmd_init_db() -> int:
    from app.infrastructure.db import make_engine
    from app.infrastructure.orm import Base

    settings = get_settings()
    if not settings.database_url.startswith("sqlite"):
        print("init-db es solo para SQLite de desarrollo; en PostgreSQL use 'migrate' (Alembic).", file=sys.stderr)
        return 2
    Base.metadata.create_all(make_engine(settings.database_url))
    print("tablas creadas")
    return 0


def cmd_replay(extra_cycles: int) -> int:
    from app.bootstrap import build_worker
    from app.infrastructure import read_models as rm
    from app.infrastructure.clock import ReplayClock
    from app.infrastructure.db import make_session_factory

    settings = get_settings()
    if settings.data_mode != "fixtures":
        print("replay requiere DATA_MODE=fixtures", file=sys.stderr)
        return 2
    worker, extras = build_worker(settings)
    worker.bootstrap()
    clock = worker.clock
    assert isinstance(clock, ReplayClock)
    cycles = 0
    while not clock.caught_up and cycles < 100_000:
        worker.run_cycle()
        cycles += 1
    for _ in range(extra_cycles):  # deja correr las últimas salidas y reintentos
        worker.run_cycle()
        cycles += 1
    print(f"{cycles} ciclos; reloj de reproducción en {clock.now().isoformat()}")

    session = make_session_factory(extras["engine"])()
    try:
        as_of = rm.system_now(session)
        data = rm.list_candidates(session, status="all", limit=200, offset=0, as_of=as_of)
        print(f"\n{'símbolo':8} {'estado':13} {'puntaje':>8}  motivos")
        for item in data["items"]:
            score = f"{float(item['score']):.2f}" if item["score"] else "-"
            print(f"{item['symbol'] or item['mint'][:6]:8} {item['status']:13} {score:>8}  {','.join(r['code'] for r in item['reasons'])}")
        for sess in rm.list_sessions(session):
            m = rm.session_metrics(session, session.get(rm.orm.PaperSession, sess["id"]), None, None)
            print(
                f"\nsesión {sess['policy']:9} cerradas={m['trades_closed']} abiertas={m['trades_open']} "
                f"ilíquidas={m['trades_illiquid']} PnL realizado={int(m['realized_pnl_lamports']) / 1e9:+.6f} SOL "
                f"abiertas valoradas={int(m['open_pnl_valued_lamports']) / 1e9:+.6f} "
                f"total={int(m['total_pnl_valued_lamports']) / 1e9:+.6f}"
            )
    finally:
        session.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("init-db")
    rp = sub.add_parser("replay")
    rp.add_argument("--extra-cycles", type=int, default=12)
    args = parser.parse_args()
    configure_logging(get_settings().log_level)
    if args.cmd == "migrate":
        return cmd_migrate()
    if args.cmd == "init-db":
        return cmd_init_db()
    return cmd_replay(args.extra_cycles)


if __name__ == "__main__":
    raise SystemExit(main())
