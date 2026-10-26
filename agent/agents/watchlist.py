"""Watchlist: links from anyone involved to SEBI orders before as_of (flag F11).

Uses the shared `watchlist_links`, so the backtest sees exactly what a live
run sees. Only reviewed links (Nemotron's automated review, or a person's)
become a flag. Unreviewed links and near-name matches become questions,
worded neutrally.
"""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent.features import watchlist_features, watchlist_links


class Watchlist(Agent):
    name = "Watchlist"
    role = ("Matches every named party against SEBI orders dated before as_of, directly "
            "and through one other entity")

    def run(self, ctx: RunContext) -> AgentResult:
        links = watchlist_links(ctx.port, ctx.ipo_id, ctx.as_of, ctx.named)
        ctx.features |= watchlist_features(links)
        case = ctx.case
        for link in links:
            o, ref = link.order, f"sebi:{link.order['order_id']}"
            order_ent = case.add_entity("sebi_order", f"{o['order_id']} {(o['title'] or '')[:80]}",
                                        source=ref, agent=self.name,
                                        key=("sebi", o["order_id"]),
                                        attrs={"date": str(o["order_date"]), "url": o["url"],
                                               "type": o["order_type"]})
            party = (case.company if link.named.role == "company" else
                     case.add_entity(link.named.role, link.named.name,
                                     source=link.named.source or ref, agent=self.name))
            case.link(party, "named_in_order" if link.hop == 1 else "linked_to_order", order_ent,
                      source=ref, agent=self.name, attrs={"hop": link.hop, "via": link.via,
                                                          "status": link.status})
            if not link.confirmed:
                how = (f"named in SEBI order {o['order_id']} ({o['order_date']})" if link.hop == 1
                       else f"{'led' if link.named.role == 'banker' else 'was behind'} {link.via}, "
                            f"which is named in SEBI order {o['order_id']} ({o['order_date']})")
                why = ("the name is a near match only" if link.status == "near_match"
                       else "the link has not been reviewed yet")
                case.add_question(f"{link.named.name} ({link.named.role}) appears to be {how}; "
                                  f"{why}. Ask whether this is the same entity.",
                                  agent=self.name, source=ref)
        confirmed = [l for l in links if l.confirmed]
        if confirmed:
            hop = min(l.hop for l in confirmed)
            worst = next(l for l in confirmed if l.hop == hop)
            text = (f"{worst.named.name} ({worst.named.role}) is "
                    + ("named in" if hop == 1 else f"linked through {worst.via} to")
                    + f" SEBI order {worst.order['order_id']} dated {worst.order['order_date']}, "
                    f"before the bid date. Being named in an order is not a finding against "
                    f"this company.")
            case.add_flag("F11", "HIGH" if hop == 1 else "ELEVATED", value={
                "links": len(confirmed), "min_hop": hop},
                evidence=[f"sebi:{l.order['order_id']}" for l in confirmed][:10] +
                         ctx.refs("sebi_named_before")[:1],
                confidence=0.9 if hop == 1 else 0.6, agent=self.name, text=text)
        elif not links:
            case.add_flag("F11", "LOW", value={"links": 0}, evidence=ctx.refs("sebi_named_before"),
                          confidence=0.7, agent=self.name,
                          text=f"No party is linked to a SEBI order dated before {ctx.as_of} "
                               "(directly or through a lead manager's earlier issue).")
        else:
            case.add_not_checked("F11 watchlist", f"{len(links)} possible link(s) not "
                                 "confirmed by review; see the questions", agent=self.name)
        return AgentResult(f"{len(confirmed)} confirmed, {len(links) - len(confirmed)} unconfirmed "
                           "link(s)", target="Planner")
