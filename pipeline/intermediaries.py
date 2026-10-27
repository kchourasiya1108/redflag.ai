"""Stage (S03): registrar and market maker masters, for both exchanges.

Registrars resolve against SEBI's register of Registrars to an Issue
(intmId=10), the same way lead managers resolve against the merchant banker
register. Market makers are stockbrokers; SEBI's broker register is too large
to page through, so they are clustered by name only (no registration number).
Runs after offer_facts, which reads market makers from the offer documents.
"""
from __future__ import annotations

import csv

from pipeline.base import Gate, StageContext
from pipeline.universe import Resolver, load_sebi_register


def run(ctx: StageContext) -> dict:
    conn = ctx.conn
    manual = load_manual_aliases(ctx.settings.data / "manual" / "intermediary_aliases.csv")
    # Re-resolve from scratch so better matching rules apply to every name.
    for table in ("ipo_market_maker", "mm_alias", "market_makers", "registrar_alias"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("UPDATE ipos SET registrar_id = NULL")
    conn.execute("DELETE FROM registrars")
    registrars = Resolver(ctx, load_sebi_register(ctx, intm_id=10), kind="registrar",
                          table="registrars", id_col="registrar_id",
                          alias_table="registrar_alias", manual=manual.get("registrar"))
    makers = Resolver(ctx, {}, kind="market_maker", table="market_makers", id_col="mm_id",
                      alias_table="mm_alias", manual=manual.get("market_maker"))
    rows = conn.execute("SELECT ipo_id, registrar_raw, market_maker_raw FROM ipos").fetchall()
    conn.execute("DELETE FROM ipo_market_maker")
    for r in rows:
        reg_id = registrars.resolve(r["registrar_raw"]) if r["registrar_raw"] else None
        conn.execute("UPDATE ipos SET registrar_id=? WHERE ipo_id=?", (reg_id, r["ipo_id"]))
        if r["market_maker_raw"] and (mm_id := makers.resolve(r["market_maker_raw"])):
            conn.execute("INSERT INTO ipo_market_maker VALUES (?,?)", (r["ipo_id"], mm_id))
    conn.commit()
    summary = {
        "registrars": conn.execute("SELECT count(*) FROM registrars").fetchone()[0],
        "registrars_with_sebi_no": conn.execute(
            "SELECT count(*) FROM registrars WHERE sebi_reg_no IS NOT NULL").fetchone()[0],
        "market_makers": conn.execute("SELECT count(*) FROM market_makers").fetchone()[0],
        "review": registrars.review + makers.review}
    ctx.log.info(f"{summary['registrars']} registrars ({summary['registrars_with_sebi_no']} "
                 f"SEBI-registered), {summary['market_makers']} market makers", **summary)
    return summary


def load_manual_aliases(path) -> dict[str, dict[str, str]]:
    """data/manual/intermediary_aliases.csv: renames a person has confirmed."""
    out: dict[str, dict[str, str]] = {}
    if path.exists():
        for r in csv.DictReader(path.open()):
            out.setdefault(r["kind"], {})[r["alias"]] = r["canonical"]
    return out


def gate(conn, settings) -> Gate:
    g = Gate("intermediaries")
    raw, mapped = conn.execute("SELECT count(registrar_raw), count(registrar_id) "
                               "FROM ipos").fetchone()
    g.add("every registrar name mapped", mapped == raw, f"{mapped}/{raw}")
    reg = conn.execute("SELECT count(*), sum(r.sebi_reg_no IS NOT NULL) FROM ipos i "
                       "JOIN registrars r USING (registrar_id)").fetchone()
    g.add("IPOs whose registrar is SEBI-registered >= 95%", (reg[1] or 0) >= 0.95 * max(reg[0], 1),
          f"{reg[1]}/{reg[0]}")
    mm_raw = conn.execute("SELECT count(market_maker_raw) FROM ipos").fetchone()[0]
    mm = conn.execute("SELECT count(*) FROM ipo_market_maker").fetchone()[0]
    g.add("every market maker name mapped", mm == mm_raw, f"{mm}/{mm_raw}")
    return g
