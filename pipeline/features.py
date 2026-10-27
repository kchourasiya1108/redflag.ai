"""Stage 08: compute_features for every labelled IPO, as of its bid date.

Runs through the Data Port with the SQLite stand-in for Zetaris, using the
same templates the live agents send to Zetaris.
"""
from __future__ import annotations

from pipeline.common.db import upsert
from agent.tools_zetaris import DataPort, MockZetaris
from agent.features import compute_features
from pipeline.base import Gate, StageContext


def run(ctx: StageContext) -> dict:
    client = MockZetaris(ctx.settings.archive_db)
    ipos = DataPort(client, None, offline=True).run("labelled_ipos").rows
    if ctx.limit:
        ipos = ipos[:ctx.limit]
    rows, dropped = [], 0

    def count_drops(result):
        nonlocal dropped
        dropped += result.dropped_by_guard

    for ipo in ipos:
        as_of = ipo["open_date"]
        port = DataPort(client, as_of, on_query=count_drops)
        try:
            feats = compute_features(ipo["ipo_id"], as_of, port)
        except Exception as e:
            ctx.log.failure(ipo["ipo_id"], "features", f"{type(e).__name__}: {e}")
            continue
        rows += [{"ipo_id": ipo["ipo_id"], "as_of": as_of.isoformat(), "name": k,
                  "value": v} for k, v in feats.items()]
    ctx.conn.execute("DELETE FROM features")
    upsert(ctx.conn, "features", rows, ("ipo_id", "as_of", "name"))
    ctx.conn.commit()
    if dropped:
        ctx.log.warn(f"leakage guard dropped {dropped} rows: a template's SQL "
                     "is missing its time filter")
    summary = {"ipos": len(ipos), "values": len(rows), "guard_dropped": dropped}
    ctx.log.info(f"features for {len(ipos)} IPOs", **summary)
    return summary


def gate(conn, settings) -> Gate:
    g = Gate("features")
    n = conn.execute("SELECT count(DISTINCT ipo_id) FROM features").fetchone()[0]
    labelled = conn.execute("SELECT count(*) FROM labels").fetchone()[0]
    g.add("features for every labelled IPO", n >= labelled > 0, f"{n}/{labelled}")
    # The leakage unit test lives in tests/test_data_port.py; the gate checks
    # the data itself: no banker history may include an outcome from after as_of.
    # f_banker_n belongs to the IPO's riskiest lead manager, so it can never
    # exceed the largest as_of-valid history among the IPO's lead managers.
    leaks = conn.execute(
        """SELECT count(*) FROM features f
           WHERE f.name = 'f_banker_n' AND f.value > (
             SELECT max(n) FROM (
               SELECT (SELECT count(*) FROM ipo_banker ib2
                       JOIN labels l ON l.ipo_id = ib2.ipo_id
                       WHERE ib2.banker_id = ib.banker_id AND ib2.ipo_id != f.ipo_id
                         AND l.outcome_known_on < f.as_of) AS n
               FROM ipo_banker ib WHERE ib.ipo_id = f.ipo_id))""").fetchone()[0]
    g.add("no banker history from after as_of", leaks == 0, f"{leaks} violations")
    return g
