from __future__ import annotations

import csv
from pathlib import Path

from ..schema_model import SchemaModel


def write_csv(schema: SchemaModel, generated: dict[str, list[dict]], order: list[str], out_dir: str) -> list[str]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    written = []
    for table_name in order:
        rows = generated[table_name]
        table = schema.tables[table_name]
        file_path = out_path / f"{table_name}.csv"
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=table.column_names())
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        written.append(str(file_path))
    return written
