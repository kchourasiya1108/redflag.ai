"""Stage 03: outcome labels for past IPOs (data guide section 11).

Labels are only for training and testing; they are never inputs for the IPO
being judged. `outcome_known_on` is the date of the +126th trading day: a
past IPO counts toward a banker's track record only if that date is before
the as_of of the IPO being judged.
"""
from __future__ import annotations

from bisect import bisect_left

from redflag.common.db import upsert
from redflag.pipeline.base import Gate, StageContext
from redflag.pipeline.prices import PRICE_EXCHANGE, daily_closes
from redflag.pipeline.sme_index import INDEX, index_close_on_or_before

HORIZON = 126            # trading days, about 6 months
CRASH_AT = -0.40         # Label A: lost more than 40% of the issue price
# Label A variant: ended 40%+ below the SME index over the same window, i.e.
# (1 + ret_6m) / (1 + index_ret_6m) - 1 < CRASH_AT. Separates a bad company
# from a bad market. Not part of `adverse`; kept for comparison.
DEAD_WINDOW = 21         # no trade in the last ~month of the window -> suspended


def run(ctx: StageContext) -> dict:
    conn = ctx.conn
    calendars = {ex: [r[0] for r in conn.execute(
        "SELECT date FROM trading_days WHERE exchange=? ORDER BY date", (ex,))]
        for ex in PRICE_EXCHANGE.values()}
    ipos = conn.execute(
        """SELECT ipo_id, exchange, symbol, company, issue_price, listing_date FROM ipos
           WHERE status='listed' AND listing_date IS NOT NULL
             AND issue_price IS NOT NULL""").fetchall()
    sebi = sebi_actions(conn)
    rows, pending = [], 0
    for ipo in ipos:
        cal = calendars.get(PRICE_EXCHANGE.get(ipo["exchange"], ""), [])
        label = compute_label(conn, cal, ipo, sebi.get(ipo["ipo_id"]))
        if label is None:
            pending += 1
            continue
        if label["close_6m"] is None and not label["suspended"]:
            ctx.log.failure(ipo["ipo_id"], "no_prices_after_listing")
        rows.append(label)
    upsert(conn, "labels", rows, ("ipo_id",))
    conn.commit()
    adverse = sum(r["adverse"] or 0 for r in rows)
    summary = {"labelled": len(rows), "too_recent": pending, "adverse": adverse,
               "crashed": sum(r["crashed"] or 0 for r in rows),
               "suspended": sum(r["suspended"] for r in rows),
               "crashed_vs_index": sum(r["crashed_vs_index"] or 0 for r in rows),
               "sebi_action": sum(r["sebi_action"] for r in rows)}
    ctx.log.info(f"labelled {len(rows)} IPOs ({adverse} adverse); "
                 f"{pending} too recent", **summary)
    return summary


def compute_label(conn, cal: list[str], ipo, sebi_date: str | None) -> dict | None:
    """Label one IPO, or None if its 6-month outcome is not known yet."""
    start = bisect_left(cal, ipo["listing_date"])
    end = start + HORIZON
    if end >= len(cal):
        return None
    horizon_day = cal[end]
    exchange = PRICE_EXCHANGE[ipo["exchange"]]
    closes = [c for c in daily_closes(conn, ipo["symbol"], exchange)
              if ipo["listing_date"] <= c[0] <= horizon_day and c[1]]
    traded = [c for c in closes if c[3] > 0]
    last = traded[-1] if traded else (closes[-1] if closes else None)
    factor = adjustment_factor(conn, exchange, ipo["symbol"], ipo["listing_date"], horizon_day)
    ret = (last[1] * factor / ipo["issue_price"] - 1) if last else None
    suspended = not traded or traded[-1][0] < cal[end - DEAD_WINDOW]
    crashed = ret is not None and ret < CRASH_AT
    sebi_action = sebi_date is not None
    idx_start = index_close_on_or_before(conn, ipo["listing_date"])
    idx_end = index_close_on_or_before(conn, horizon_day)
    idx_ret = idx_end / idx_start - 1 if idx_start and idx_end else None
    rel_crash = (int((1 + ret) / (1 + idx_ret) - 1 < CRASH_AT)
                 if ret is not None and idx_ret is not None else None)
    return {
        "ipo_id": ipo["ipo_id"], "listing_date": ipo["listing_date"],
        "issue_price": ipo["issue_price"],
        "close_6m": last[1] if last else None,
        "close_6m_date": last[0] if last else None,
        "outcome_known_on": horizon_day,
        "adj_factor": factor,
        "ret_6m": round(ret, 6) if ret is not None else None,
        "crashed": int(crashed) if ret is not None else None,
        "suspended": int(suspended),
        "stale_close": int(bool(last) and last[0] < cal[end - 5]),
        "sebi_action": int(sebi_action),
        "sebi_action_date": sebi_date,
        "adverse": int(crashed or suspended or sebi_action),
        "index_name": INDEX if idx_ret is not None else None,
        "index_ret_6m": round(idx_ret, 6) if idx_ret is not None else None,
        "crashed_vs_index": rel_crash,
    }


def adjustment_factor(conn, exchange: str, symbol: str, after: str, through: str) -> float:
    """Shares now held per IPO share: 2.0 after a 1:1 bonus."""
    factor = 1.0
    for (ratio,) in conn.execute(
            "SELECT ratio FROM corp_actions WHERE exchange=? AND symbol=? "
            "AND ex_date > ? AND ex_date <= ?", (exchange, symbol, after, through)):
        factor /= ratio
    return factor


def sebi_actions(conn) -> dict[str, str]:
    """Label B: the first SEBI order after listing naming the company.

    Filled once the SEBI stage has mapped order entities to IPO companies;
    until then every IPO has sebi_action = 0.
    """
    return {r["ipo_id"]: r["first_order"] for r in conn.execute(
        """SELECT i.ipo_id, min(o.order_date) AS first_order
           FROM ipos i
           JOIN entity_alias a ON a.entity_id = 'ent:company:' || i.ipo_id
           JOIN sebi_order_entity e ON e.entity_id = a.entity_id
           JOIN sebi_orders o ON o.order_id = e.order_id
           WHERE o.order_date > i.listing_date AND e.reviewed = 1
           GROUP BY i.ipo_id""")}


def explain(conn, ipo_id: str) -> str:
    """The numbers behind one label, for the hand-check stage gate."""
    label = conn.execute("SELECT * FROM labels WHERE ipo_id=?", (ipo_id,)).fetchone()
    ipo = conn.execute("SELECT symbol FROM ipos WHERE ipo_id=?", (ipo_id,)).fetchone()
    if not label or not ipo:
        return f"{ipo_id}: no label (unknown IPO, or outcome not known yet)"
    actions = conn.execute(
        "SELECT ex_date, ratio FROM corp_actions WHERE symbol=? AND ex_date > ? "
        "AND ex_date <= ?", (ipo["symbol"], label["listing_date"],
                             label["outcome_known_on"])).fetchall()
    lines = [
        f"{ipo_id} ({ipo['symbol']})",
        f"  issue price            {label['issue_price']}",
        f"  listing date           {label['listing_date']}",
        f"  +{HORIZON} trading days      {label['outcome_known_on']}",
        f"  close used             {label['close_6m']} on {label['close_6m_date']}",
        f"  corporate actions      {[tuple(a) for a in actions] or 'none'}",
        f"  shares per IPO share   {label['adj_factor']}",
        f"  ret_6m = {label['close_6m']} x {label['adj_factor']} / "
        f"{label['issue_price']} - 1 = {label['ret_6m']}",
        f"  crashed={label['crashed']} suspended={label['suspended']} "
        f"sebi_action={label['sebi_action']} adverse={label['adverse']}",
    ]
    return "\n".join(lines)


def gate(conn, settings) -> Gate:
    g = Gate("labels")
    n, with_ret = conn.execute(
        "SELECT count(*), count(ret_6m) FROM labels").fetchone()
    g.add("labelled", n > 0, f"{n} IPOs labelled")
    g.add("returns computed >= 90%", with_ret >= 0.9 * max(n, 1),
          f"{with_ret}/{n} have a 6-month return")
    note = settings.data / "gates" / "labels_handcheck.md"
    g.add("3 hand-computed returns match", note.exists(),
          f"manual; record it in {note.name}")
    return g
