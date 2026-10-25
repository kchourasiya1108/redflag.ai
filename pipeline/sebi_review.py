"""Automated review of SEBI order links (who is named in each order), by Nemotron.

The user's decision (25 Oct 2026): Nemotron gives the final review label for
each proposed `sebi_order_entity` row, with no human step. For each pending
row the model reads the passages of the order around the entity's name and
the archive's description of the entity, and answers two questions: is this
the same entity, and is it a subject of the order (a noticee or the matter
itself) rather than merely mentioned? Only a "same entity, subject" answer
sets reviewed = 1; anything else sets -1. The verdict, the model that served
it and its reason are stored (`reviewer`, `review_reason`), so every report
can say a link was confirmed by automated review, and a person can still
overrule any row with `sebi-review import`.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pipeline.common import names

SYSTEM = ("You review links between Indian regulatory orders and companies or merchant "
          "bankers. Decide only from the order text given. Be strict: the same name in a "
          "different company, or a passing mention, is not a link.")


class Review(BaseModel):
    same_entity: bool
    role_in_order: Literal["noticee", "subject", "mentioned", "not_found"]
    finding: str = Field(default="", max_length=300)
    action: str = Field(default="", max_length=300)
    reason: str = Field(default="", max_length=400)


def passages(text: str, name: str, budget: int = 7000) -> str:
    """The order's opening plus the passages around each mention of the entity."""
    core = names.normalise(name).split()
    key = " ".join(core[:2]) if core else name.lower()
    flat = re.sub(r"\s+", " ", text)
    out = [flat[:2500]]
    for m in re.finditer(re.escape(key), flat.lower()):
        out.append("... " + flat[max(0, m.start() - 700):m.end() + 900] + " ...")
        if sum(map(len, out)) > budget:
            break
    return "\n\n".join(out)[:budget]


def describe(conn, row) -> str:
    if row["entity_role"] == "company" and row["entity_id"]:
        ipo = conn.execute("SELECT company, exchange, open_date, listing_date FROM ipos "
                           "WHERE ipo_id = ?", (row["entity_id"].rsplit(":", 1)[1],)).fetchone()
        if ipo:
            return (f"the SME IPO company '{ipo['company']}', which listed on "
                    f"{ipo['exchange']} on {ipo['listing_date'] or ipo['open_date']}")
    if row["entity_role"] == "banker" and row["entity_id"]:
        b = conn.execute("SELECT canonical_name, sebi_reg_no FROM bankers WHERE banker_id = ?",
                         (int(row["entity_id"].rsplit(":", 1)[1]),)).fetchone()
        if b:
            return (f"the SEBI-registered merchant banker '{b['canonical_name']}'"
                    + (f" (registration {b['sebi_reg_no']})" if b["sebi_reg_no"] else ""))
    return f"the {row['entity_role']} '{row['entity_name']}'"


def review_one(gateway, conn_factory, cache: Path, row: dict) -> tuple[dict, Review, str]:
    path = cache / "sebi_text" / f"{row['order_id']}.txt"
    text = path.read_text(errors="ignore") if path.exists() else ""
    conn = conn_factory()
    try:
        who = describe(conn, row)
    finally:
        conn.close()
    body = (f"Order text (opening and every passage naming it):\n"
            f"{passages(text, row['entity_name'])}" if len(text.strip()) >= 200 else
            "The order's text could not be extracted (a scanned PDF); decide from the title "
            "alone, and say so in the reason.")
    prompt = (f"SEBI order {row['order_id']} ({row['order_date']}): {row['title']}\n\n"
              f"Archive entity: {who}. The order was matched to it through the name "
              f"'{row['entity_name']}'.\n\n{body}\n\n"
              "Is the entity in the order the same as the archive entity? Is it a noticee or "
              "the subject of the order, or only mentioned? Give the finding and the action "
              "against it in a few words each (empty if none), and a one-sentence reason.")
    res = gateway.generate(prompt, SYSTEM, schema=Review, route="extract",
                           purpose=f"sebi_review:{row['order_id']}")
    return row, res.parsed, res.served_model


def model_review(conn, conn_factory, gateway, cache: Path, *, workers: int = 3,
                 limit: int | None = None, log=print) -> dict:
    rows = [dict(r) for r in conn.execute(
        """SELECT e.order_id, e.entity_name, e.entity_role, e.entity_id, o.order_date, o.title
           FROM sebi_order_entity e JOIN sebi_orders o USING (order_id)
           WHERE e.reviewed = 0 AND e.reviewer IS NULL ORDER BY o.order_date""")]
    rows = rows[:limit] if limit else rows
    done = {"confirmed": 0, "rejected": 0, "unresolved": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(review_one, gateway, conn_factory, cache, r) for r in rows]
        for fut in as_completed(futures):
            try:
                row, verdict, model = fut.result()
            except Exception as e:      # one bad row never stops the review
                done["failed"] += 1
                log(f"FAIL {e}"[:200])
                continue
            ok = verdict.same_entity and verdict.role_in_order in ("noticee", "subject")
            # "not_found" means the model could not tell (a scanned order): leave the
            # row pending rather than drop a possibly real link for good.
            label = 1 if ok else 0 if verdict.role_in_order == "not_found" else -1
            conn.execute(
                """UPDATE sebi_order_entity SET reviewed = ?, finding = ?, action = ?,
                   reviewer = ?, review_reason = ? WHERE order_id = ? AND entity_name = ?
                   AND entity_role = ?""",
                (label, verdict.finding or None, verdict.action or None,
                 f"nemotron:{model}", f"{verdict.role_in_order}: {verdict.reason}"[:500],
                 row["order_id"], row["entity_name"], row["entity_role"]))
            conn.commit()
            done["confirmed" if ok else "unresolved" if label == 0 else "rejected"] += 1
            log(f"{'ok ' if ok else '?? ' if label == 0 else 'no '} {row['order_id']} "
                f"{row['entity_name'][:40]}: "
                f"{verdict.role_in_order}; {verdict.reason[:90]}")
    return done
