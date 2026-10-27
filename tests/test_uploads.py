"""Uploaded RHPs (agent/uploads.py): an upcoming IPO checked from the investor's PDF."""
import pytest

from agent import uploads
from tests.test_agents import sample_cfg  # noqa: F401  (fixture)
from tests.test_web_api import _wait, client  # noqa: F401  (fixture)

COVER = [
    "RED HERRING PROSPECTUS Dated: October 20, 2026",
    "100% Book Built Issue",
    "ACME PRECISION TOOLS LIMITED",
    "Corporate Identification Number: U29100GJ2015PLC012345",
    "BID/ISSUE PROGRAMME",
    "ANCHOR INVESTOR BIDDING DATE: OCTOBER 29, 2026",
    "BID/ISSUE OPENS ON: OCTOBER 30, 2026",
    "BID/ISSUE CLOSES ON: NOVEMBER 03, 2026",
    "The Equity Shares are proposed to be listed on the SME Platform of NSE (NSE EMERGE).",
    "BOOK RUNNING LEAD MANAGER TO THE ISSUE",
    "FIRST OVERSEAS CAPITAL LIMITED",
    "REGISTRAR TO THE ISSUE",
    "BIGSHARE SERVICES PRIVATE LIMITED",
]


def make_pdf(pages: list[list[str]]) -> bytes:
    """A small PDF with a real text layer, one list of lines per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        ops = "BT /F1 9 Tf 40 800 Td 12 TL " + " ".join(
            "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") '"
            for line in lines) + " ET"
        objs.append(f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


FILLER = ["This page describes the business of the company in some detail for investors."] * 4


@pytest.fixture
def rhp_pdf():
    return make_pdf([COVER, FILLER, FILLER])


def test_cover_facts_read_the_cover():
    facts = uploads.cover_facts(["\n".join(COVER)])
    assert facts["company"] == "ACME PRECISION TOOLS LIMITED"
    assert facts["open_date"] == "2026-10-30"           # the anchor day is not the bid date
    assert facts["exchange"] == "NSE Emerge"
    assert facts["issue_price"] is None                  # book built: entered by the investor
    assert facts["lead_managers"] == ["FIRST OVERSEAS CAPITAL LIMITED"]


def test_as_of_is_today_before_the_bid():
    from datetime import date
    rec = {"fields": {"open_date": "2026-10-30"}}
    assert uploads.as_of(rec, today=date(2026, 10, 26)) == date(2026, 10, 26)
    assert uploads.as_of(rec, today=date(2026, 11, 5)) == date(2026, 10, 30)


def test_upload_confirm_and_check(client, rhp_pdf):  # noqa: F811
    assert client.post("/api/uploads", content=b"not a pdf",
                       headers={"Content-Type": "application/pdf"}).status_code == 422
    up = client.post("/api/uploads", content=rhp_pdf, headers={
        "Content-Type": "application/pdf", "X-Filename": "Acme%20RHP.pdf"}).json()
    uid = up["upload_id"]
    assert uid.startswith("UP-") and up["status"] == "ok" and up["filename"] == "Acme RHP.pdf"
    assert up["fields"]["company"] == "ACME PRECISION TOOLS LIMITED"
    assert up["missing"] == [] and "issue_price" not in up["fields"]   # [●]: not announced yet
    assert up["field_sources"]["open_date"] == "the RHP cover"
    # The same file again is the same upload.
    again = client.post("/api/uploads", content=rhp_pdf, headers={"Content-Type": "application/pdf"})
    assert again.json()["upload_id"] == uid
    assert client.patch(f"/api/uploads/{uid}", json={"issue_price": -5}).status_code == 422
    assert client.patch(f"/api/uploads/{uid}", json={"exchange": "LSE"}).status_code == 422
    done = client.patch(f"/api/uploads/{uid}", json={"issue_price": 120, "cin": "ignored"}).json()
    assert done["fields"]["issue_price"] == 120.0 and done["field_sources"]["issue_price"] == "you"
    # Clearing an edit goes back to what was filled in.
    back = client.patch(f"/api/uploads/{uid}", json={"issue_price": None}).json()
    assert "issue_price" not in back["fields"] and back["edits"] == {}
    client.patch(f"/api/uploads/{uid}", json={"issue_price": 120})

    detail = client.get(f"/api/ipos/{uid}").json()
    assert detail["uploaded"] and detail["ipo"]["company"].startswith("ACME")
    assert client.get(f"/api/ipos/{uid}/pages/1").json()["pages"] == 3
    assert any(u["upload_id"] == uid for u in client.get("/api/uploads").json()["items"])

    run_id = client.post("/api/runs", json={"ipo_id": uid, "sample_mode": True}).json()["run_id"]
    state = _wait(client, run_id)
    assert state["status"] == "success", state.get("error")
    out = state["result"]["output"]
    assert out["ipo"]["ipo_id"] == uid and out["ipo"]["company"].startswith("ACME")
    trace = client.get(f"/api/runs/{run_id}/trace").json()
    served = {e["output_summary"].split(" ->")[0]: e["detail"]["source"]
              for e in trace if e["action"] == "data.query"}
    assert served["ipo_bankers"] == "uploaded-rhp" and served["ipo_rhp_doc"] == "uploaded-rhp"
    assert served["sebi_named_before"] != "uploaded-rhp"     # history still from the data layer
    # The lead manager printed on the cover was matched to the archive's firm.
    assert served["banker_names"] != "uploaded-rhp" and "banker_track_record" in served


def test_unknown_and_bad_upload_ids(client):  # noqa: F811
    assert client.get("/api/uploads/UP-00000000").status_code == 404
    assert client.get("/api/uploads/UP-../../etc").status_code == 404
    assert client.get("/api/ipos/UP-00000000").status_code == 404


class FakeFetcher:
    """Serves canned exchange responses by URL substring."""

    def __init__(self, routes: dict):
        self.routes = routes

    def get(self, url, dest=None, *, referer=None):
        for key, body in self.routes.items():
            if key in url:
                return body if isinstance(body, bytes) else body.encode()
        raise AssertionError(f"unexpected fetch {url}")


def test_upcoming_lists_only_open_sme_issues_and_loads_their_rhps(sample_cfg, rhp_pdf):  # noqa: F811
    import json as _json
    from pipeline import upcoming
    nse = _json.dumps([
        {"symbol": "BIGCO", "series": "EQ", "companyName": "Big Co Limited",
         "issueStartDate": "29-Oct-2026", "issueEndDate": "31-Oct-2026", "issuePrice": "Rs.78 to Rs.82"},
        {"symbol": "ACME", "series": "SME", "companyName": "Acme Precision Tools Limited",
         "issueStartDate": "30-Oct-2026", "issueEndDate": "03-Nov-2026", "issuePrice": "Rs.95 to Rs.100",
         "status": "Forthcoming"}])
    detail = _json.dumps({"issueInfo": {"dataList": [
        {"title": "Book Running Lead Managers", "value": "First Overseas Capital Limited"},
        {"title": "Red Herring Prospectus", "value": "https://archives.nseindia.com/content/ipo/RHP_ACME.pdf"},
        {"title": "Bid Lot", "value": "1200 Equity Shares"}]}})
    bse_index = ("<table><tr><td>Old Co Limited</td><td><a href='/download/1/a/d.pdf'>D</a> 01/01/2025</td>"
                 "<td><a href='/download/1/a/r.pdf'>R</a></td><td><a href='/download/1/a/p.pdf'>P</a></td>"
                 "<td><a href='/download/1/a/b.pdf'>B</a></td></tr>"
                 "<tr><td>New Bse Co Limited</td><td><a href='/download/9/a/d.pdf'>D</a> 01/09/2026</td>"
                 "<td><a href='/download/9/a/r.pdf'>R</a></td><td></td><td></td></tr></table>")
    fetcher = FakeFetcher({"all-upcoming-issues": nse, "ipo-detail": detail, "RHP_ACME": rhp_pdf,
                           "SMEIPODRHP": bse_index, "/download/9/": make_pdf([COVER[:3] + FILLER, FILLER])})
    summary = upcoming.refresh(sample_cfg, fetcher, log=lambda *_: None)
    assert summary["errors"] == [] and len(summary["found"]) == 2
    records = [uploads.load(sample_cfg, i) for i in summary["found"]]
    nse_rec = next(r for r in records if r["source"]["exchange"] == "NSE Emerge")
    assert nse_rec["fields"]["issue_price"] == 100.0           # top of NSE's price band
    assert nse_rec["fields"]["open_date"] == "2026-10-30" and nse_rec["fields"]["lot_size"] == 1200
    assert uploads.missing(nse_rec) == []
    bse_rec = next(r for r in records if r["source"]["exchange"] == "BSE SME")
    assert bse_rec["source"]["symbol"] == "9"                   # closed "Old Co" is skipped
    # A second refresh finds the same PDFs and keeps the investor's edits.
    uploads.update(sample_cfg, nse_rec["upload_id"], {"issue_price": 97})
    upcoming.refresh(sample_cfg, fetcher, log=lambda *_: None)
    assert uploads.load(sample_cfg, nse_rec["upload_id"])["fields"]["issue_price"] == 97.0


def test_prefill_takes_a_known_ipo_from_the_archive(sample_cfg):  # noqa: F811
    from agent import prefill
    rec = {"detected": {"company": "SYNOPTICS TECHNOLOGIES LIMITED", "cin": "U72900MH2008PLC187575"}}
    fields, prov, _ = prefill.prefill(sample_cfg, rec, [])
    assert fields["open_date"] == "2023-06-30" and fields["issue_price"] == 237.0
    assert fields["lead_managers"] == ["First Overseas Capital Limited"] and prov["issue_price"] == "archive"


class FakeGateway:
    def __init__(self, reply):
        self.reply = reply

    def generate(self, prompt, system=None, *, schema=None, route="", purpose=""):
        from types import SimpleNamespace
        return SimpleNamespace(parsed=schema.model_validate(self.reply))


def test_ai_cover_read_keeps_only_quoted_values():
    from agent import prefill
    pages = ["\n".join(COVER + ["Price Band: Rs. 95 to Rs. 100 per Equity Share",
                                "MARKET MAKER: Shreni Shares Limited"])]
    reply = {
        "issue_price": {"value": "100", "quote": "Price Band: Rs. 95 to Rs. 100 per Equity Share"},
        "market_maker": {"value": "Shreni Shares Limited", "quote": "MARKET MAKER: Shreni Shares Limited"},
        "auditor": {"value": "Made Up & Co", "quote": "Statutory Auditor: Made Up & Co"},   # not on the page
        "lot_size": {"value": "1200", "quote": "Price Band: Rs. 95 to Rs. 100"},          # quote lacks 1200
    }
    got = prefill.ai_cover(FakeGateway(reply), pages, ["issue_price", "market_maker", "auditor", "lot_size"])
    assert got == {"issue_price": 100.0, "market_maker": "Shreni Shares Limited"}


def test_auto_mode_runs_offline_when_live_services_are_down(client, rhp_pdf, monkeypatch):  # noqa: F811
    import app.api as api
    monkeypatch.setattr(api, "status", lambda probe=True: {"zetaris": {"ok": False}, "meterless": {"ok": True}})
    uid = client.post("/api/uploads", content=rhp_pdf, headers={"Content-Type": "application/pdf"}).json()["upload_id"]
    run_id = client.post("/api/runs", json={"ipo_id": uid, "auto": True}).json()["run_id"]
    state = _wait(client, run_id)
    assert state["status"] == "success"
    assert "offline" in state["result"]["output"]["data_source"] or "sample" in state["result"]["output"]["data_source"]


def test_a_slow_model_never_holds_up_the_upload(sample_cfg, monkeypatch):  # noqa: F811
    import time as _time
    from agent import prefill
    monkeypatch.setattr(prefill, "AI_BUDGET_S", 0.2)

    class Slow:
        def generate(self, *a, **k):
            _time.sleep(2)

    t0 = _time.time()
    fields, _, notes = prefill.prefill(sample_cfg, {"detected": {"company": "Acme Limited"}},
                                       ["\n".join(COVER)], gateway=Slow())
    assert _time.time() - t0 < 1.5 and fields["company"] == "Acme Limited"
    assert any("no answer" in n for n in notes)


def test_a_main_board_draft_is_found_on_sebi_and_feeds_f12(sample_cfg):  # noqa: F811
    from agent import prefill

    listing = ("<tr><td>Jan 30, 2026</td><td><a href='https://www.sebi.gov.in/filings/public-issues/jan-2026/"
               "acme_1.html'>Acme Precision Tools Limited</a></td></tr>"
               "<tr><td>Nov 02, 2026</td><td><a href='https://www.sebi.gov.in/x_2.html'>"
               "Acme Precision Tools Limited</a></td></tr>")      # after the bid: not the draft
    draft = make_pdf([FILLER, ["RESTATED STATEMENT OF PROFIT AND LOSS"]])

    class Fetcher:
        got = []

        def get(self, url, dest=None, *, referer=None):
            self.got.append(url)
            if url.endswith("acme_1.html"):
                return b"<iframe src='../web/?file=https://www.sebi.gov.in/sebi_data/attachdocs/feb-2026/1.pdf'>"
            return draft if url.endswith(".pdf") else b""

        def post_form(self, url, data, *, referer, dest=None):
            return listing.encode()
    rec = {"upload_id": "UP-test0001", "fields": {"company": "ACME PRECISION TOOLS LIMITED",
                                                  "open_date": "2026-10-30"}}
    got = prefill.draft_prospectus(sample_cfg, rec, Fetcher())
    assert got["source"] == "sebi" and got["url"].endswith("feb-2026/1.pdf") and got["facts"] == []
    assert not any("x_2" in u for u in Fetcher.got)
    rec.update(sha256="x", pages=2, text_pages=2, status="ok", detected={},
               drhp={"facts": [{"field": "pl.pat", "fiscal_year": "FY25", "value": 1.0, "page": 9, "verified": 1}]})

    class Port:
        def run(self, template, **k):
            from types import SimpleNamespace
            return SimpleNamespace(rows=[])
    assert uploads.local_rows(rec, Port())["ipo_drhp_facts"][0]["field"] == "pl.pat"
