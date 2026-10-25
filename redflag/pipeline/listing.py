"""Shared by the universe, bse_universe and prices stages."""
from __future__ import annotations

# ipos.exchange -> the exchange code used in prices, trading_days and labels
PRICE_EXCHANGE = {"NSE Emerge": "NSE", "BSE SME": "BSE"}


def set_listing_open(ctx) -> None:
    """First traded price on the listing day, from the prices table.

    Called after any stage that writes ipos or prices, so the value is never
    lost when one of them is re-run.
    """
    for ipo_exchange, price_exchange in PRICE_EXCHANGE.items():
        ctx.conn.execute(
            """UPDATE ipos SET listing_open_price = (
                 SELECT p.open FROM prices p
                 WHERE p.exchange=? AND p.symbol=ipos.symbol AND p.date=ipos.listing_date
                 ORDER BY p.volume DESC LIMIT 1)
               WHERE exchange=? AND listing_date IS NOT NULL""",
            (price_exchange, ipo_exchange))


def fix_trading_symbols(ctx) -> int:
    """NSE IPOs whose list symbol never traded: find the symbol they trade under.

    NSE's IPO list sometimes names a symbol that was changed before listing
    (CML trades as CMNL). The trading symbol is the NSE SME security that first
    traded within 25 days of the issue closing, with the most similar name.
    """
    from datetime import date, timedelta
    from redflag.common import names
    conn, fixed = ctx.conn, 0
    rows = conn.execute(
        """SELECT i.ipo_id, i.company, i.close_date FROM ipos i
           WHERE i.exchange = 'NSE Emerge' AND i.status = 'listed' AND NOT EXISTS (
             SELECT 1 FROM prices p WHERE p.exchange = 'NSE' AND p.symbol = i.symbol
               AND p.date >= i.open_date)""").fetchall()
    for r in rows:
        lo = r["close_date"]
        hi = (date.fromisoformat(lo) + timedelta(days=25)).isoformat()
        best, score = None, 0.0
        for s in conn.execute(
                """SELECT s.symbol, s.first_date, group_concat(n.name, '|') AS names
                   FROM securities s JOIN security_names n USING (exchange, symbol)
                   WHERE s.exchange = 'NSE' AND s.first_date > ? AND s.first_date <= ?
                   GROUP BY s.symbol""", (lo, hi)):
            sim = max(names.similarity(r["company"], n) for n in s["names"].split("|"))
            if sim > score:
                best, score = s, sim
        if best and score >= 0.6:
            conn.execute("UPDATE ipos SET symbol = ?, listing_date = ? WHERE ipo_id = ?",
                         (best["symbol"], best["first_date"], r["ipo_id"]))
            ctx.log.event("symbol_fixed", f"{r['ipo_id']} trades as {best['symbol']} from "
                          f"{best['first_date']} (name match {score:.2f})", status="warning")
            fixed += 1
        else:
            ctx.log.failure(r["ipo_id"], "no_trading_symbol", f"best name match {score:.2f}")
    return fixed


def reconcile_issue_prices(ctx) -> int:
    """Replace an NSE issue price that lies outside its own price band.

    On listing day the exchange's previous close is the issue (base) price;
    it is used when it sits inside the band. Checked 25 Oct 2026: 404 of 406
    NSE listings agree with NSE's list; the two that don't are list typos.
    """
    conn, fixed = ctx.conn, 0
    for r in conn.execute(
            """SELECT i.ipo_id, i.issue_price, i.price_band_low, i.price_band_high,
                      (SELECT p.prev_close FROM prices p WHERE p.exchange = 'NSE'
                         AND p.symbol = i.symbol AND p.date = i.listing_date
                       ORDER BY p.volume DESC LIMIT 1) AS base
               FROM ipos i WHERE i.exchange = 'NSE Emerge' AND i.price_band_low IS NOT NULL
                 AND (i.issue_price < i.price_band_low - 0.01
                      OR i.issue_price > i.price_band_high + 0.01)""").fetchall():
        if r["base"] and r["price_band_low"] - 0.01 <= r["base"] <= r["price_band_high"] + 0.01:
            conn.execute("UPDATE ipos SET issue_price = ?, issue_price_basis = "
                         "'listing_base_price' WHERE ipo_id = ?", (r["base"], r["ipo_id"]))
            ctx.log.event("issue_price_fixed", f"{r['ipo_id']}: {r['issue_price']} is outside "
                          f"its band; listing base price {r['base']} used", status="warning")
            fixed += 1
    return fixed
