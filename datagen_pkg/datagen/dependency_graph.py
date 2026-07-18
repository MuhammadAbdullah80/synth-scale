"""Stage 2: build the table dependency graph and derive a safe generation order.

Tables are nodes; a directed edge child -> parent means "child has a FK to
parent, parent must be generated first". A normal (acyclic) schema topologically
sorts cleanly. Two situations break that:

  1. Self-referencing FK (e.g. employees.manager_id -> employees.id).
  2. A cycle between two (or more) tables via mutual FKs.

For both, we pick one FK per cycle to "defer": that FK is generated as NULL
during the main pass, and backfilled in a second pass once all tables involved
already exist. This is standard practice for seeding data into schemas with
circular references (real databases handle it the same way, via deferred
constraints or a nullable FK).
"""
from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from .schema_model import ForeignKey, SchemaModel


@dataclass
class DeferredFK:
    child_table: str
    fk: ForeignKey


@dataclass
class GenerationPlan:
    order: list[str]
    deferred_fks: list[DeferredFK]


def build_generation_plan(schema: SchemaModel) -> GenerationPlan:
    graph = nx.DiGraph()
    graph.add_nodes_from(schema.tables.keys())

    edge_to_fk: dict[tuple[str, str], list[tuple[str, ForeignKey]]] = {}
    for child_name, fk in schema.all_fks():
        if fk.ref_table not in schema.tables:
            raise ValueError(
                f"FK on {child_name}.{','.join(fk.columns)} references unknown table "
                f"{fk.ref_table!r}. Check the DDL for typos or missing CREATE TABLE."
            )
        edge = (child_name, fk.ref_table)
        edge_to_fk.setdefault(edge, []).append((child_name, fk))
        graph.add_edge(*edge)

    deferred: list[DeferredFK] = []

    # Break cycles (including self-loops) one at a time. Each break removes one
    # edge from the graph so the topo sort can proceed; the FK backing that
    # edge is generated as NULL first, then backfilled in a second pass.
    while True:
        try:
            cycle = nx.find_cycle(graph, orientation="original")
        except nx.NetworkXNoCycle:
            break

        # cycle is a list of (u, v, direction) edges; break the first one.
        u, v, _direction = cycle[0][0], cycle[0][1], cycle[0][2] if len(cycle[0]) > 2 else None
        fks_on_edge = edge_to_fk.get((u, v), [])
        if not fks_on_edge:
            # Shouldn't happen, but avoid an infinite loop if it does.
            graph.remove_edge(u, v)
            continue

        child_name, fk = fks_on_edge.pop()
        if not fks_on_edge:
            del edge_to_fk[(u, v)]
        if not fk.nullable:
            raise ValueError(
                f"Circular foreign key dependency involving {child_name}.{','.join(fk.columns)} "
                f"-> {fk.ref_table} cannot be resolved because the FK column(s) are NOT NULL. "
                f"Make the FK nullable, or restructure the schema to break the cycle."
            )
        deferred.append(DeferredFK(child_table=child_name, fk=fk))
        graph.remove_edge(u, v)

    order = list(reversed(list(nx.topological_sort(graph))))
    return GenerationPlan(order=order, deferred_fks=deferred)
