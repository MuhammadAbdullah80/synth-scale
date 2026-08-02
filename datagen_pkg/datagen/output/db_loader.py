from __future__ import annotations

from sqlalchemy import MetaData, Table as SATable, create_engine, insert

from ..dburl import normalize_db_url
from ..schema_model import SchemaModel

BATCH_SIZE = 1000


def load_to_db(schema: SchemaModel, generated: dict[str, list[dict]], order: list[str], connection_url: str) -> dict[str, int]:
    """Load generated rows directly into a live database via SQLAlchemy core,
    in FK-safe order, batched. Assumes the target tables already exist (the
    same DDL you fed the parser) -- this does not run CREATE TABLE.
    """
    engine = create_engine(normalize_db_url(connection_url))
    metadata = MetaData()
    inserted_counts: dict[str, int] = {}

    with engine.begin() as conn:
        for table_name in order:
            rows = generated[table_name]
            if not rows:
                inserted_counts[table_name] = 0
                continue
            sa_table = SATable(table_name, metadata, autoload_with=engine)
            for i in range(0, len(rows), BATCH_SIZE):
                batch = rows[i:i + BATCH_SIZE]
                conn.execute(insert(sa_table), batch)
            inserted_counts[table_name] = len(rows)

    return inserted_counts
