"""Promoters and directors (F11, label B): read from the RHP, screened against SEBI orders."""
from datetime import date
from types import SimpleNamespace

from agent.features import Named, watchlist_links
from pipeline import offer_docs

COVER = ("RED HERRING PROSPECTUS\nACME TOOLS LIMITED\n"
         "PROMOTERS OF OUR COMPANY: MR. TILAK MUNDHRA AND MS. ANJALI RAJESH JAIN\n"
         "DETAILS OF THE ISSUE TO PUBLIC\n")
BOARD = ("Our Board of Directors comprises the following: Name Designation Address\n"
         "Tilak Mundhra (DIN: 05259145) Chairman and Managing Director Ahmedabad, Gujarat, India\n"
         "Gopesh Shah (DIN: 06610935) Independent Director\n"
         "Certified by CA Kunal Beswal, UDIN: 25131054BMHZEP5061\n")


def test_promoters_and_directors_are_read_from_the_rhp():
    assert offer_docs.promoters([COVER]) == ["Tilak Mundhra", "Anjali Rajesh Jain"]
    assert offer_docs.directors([COVER, BOARD]) == ["Tilak Mundhra", "Gopesh Shah"]   # UDIN is not a DIN
    people = offer_docs.people([COVER, BOARD])
    assert {(p["name"], p["role"]) for p in people} == {
        ("Tilak Mundhra", "promoter"), ("Anjali Rajesh Jain", "promoter"),
        ("Tilak Mundhra", "director"), ("Gopesh Shah", "director")}
    assert offer_docs.person_key("MR. TILAK  MUNDHRA") == offer_docs.person_key("Tilak Mundhra")


class FakePort:
    def __init__(self, tables):
        self.tables = tables

    def run(self, template, **params):
        rows = self.tables.get(template, [])
        return SimpleNamespace(rows=rows(params) if callable(rows) else rows)


ORDER = {"order_id": "sebi-1", "order_date": date(2024, 1, 5), "order_type": "Order", "title": "t",
         "url": "", "reviewed": 1}


def test_people_match_only_people_and_reach_their_earlier_issues():
    orders = [ORDER | {"entity_name": "Tilak Mundhra", "entity_role": "person", "entity_id": None},
              ORDER | {"order_id": "sebi-2", "entity_name": "Tilak Mundhra Enterprises Ltd",
                       "entity_role": "company", "entity_id": None},
              ORDER | {"order_id": "sebi-3", "entity_name": "Old Co Ltd", "entity_role": "company",
                       "entity_id": "ent:company:NSE-2022-OLD"}]
    port = FakePort({
        "sebi_named_before": orders,
        "person_past_ipos": lambda p: ([{"ipo_id": "NSE-2022-OLD", "company": "Old Co Ltd",
                                         "open_date": date(2022, 5, 1), "role": "promoter"}]
                                       if p["person_key"] == "tilak-mundhra" else []),
        "banker_past_ipos": [],
    })
    named = [Named("promoter", "Tilak Mundhra", "ent:person:tilak-mundhra"),
             Named("director", "Tilak Mundra Kumar", "ent:person:tilak-mundra-kumar")]
    links = watchlist_links(port, "NSE-2025-NEW", date(2025, 3, 1), named)
    got = {(l.named.name, l.order["order_id"], l.hop) for l in links}
    assert ("Tilak Mundhra", "sebi-1", 1) in got            # named in the order as a person
    assert ("Tilak Mundhra", "sebi-3", 2) in got            # was behind an earlier issue named in an order
    assert not any(o == "sebi-2" for _, o, _ in got)        # a person never matches a company row
    assert not any(n == "Tilak Mundra Kumar" for n, _, _ in got)   # a near name is not the same person
