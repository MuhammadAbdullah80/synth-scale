from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from ..schema_model import SchemaModel

BATCH_SIZE = 500


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (date, datetime)):
        return f"'{value.isoformat()}'"
    # string: escape single quotes
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def write_sql(schema: SchemaModel, generated: dict[str, list[dict]], order: list[str], out_path: str) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    lines = ["BEGIN;", ""]
    for table_name in order:
        table = schema.tables[table_name]
        rows = generated[table_name]
        cols = table.column_names()
        col_list = ", ".join(f'"{c}"' for c in cols)
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            values_sql = ",\n".join(
                "  (" + ", ".join(_sql_literal(row[c]) for c in cols) + ")" for row in batch
            )
            lines.append(f'INSERT INTO "{table_name}" ({col_list}) VALUES\n{values_sql};')
            lines.append("")
    lines.append("COMMIT;")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path
