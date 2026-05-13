from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"
SCN = "grid_beta"
YEAR = 2025

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            DB_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=300,
        )
    return _engine
