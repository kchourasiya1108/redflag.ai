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
    assert up["missing"] == ["issue_price"]
    # The same file again is the same upload.
    again = client.post("/api/uploads", content=rhp_pdf, headers={"Content-Type": "application/pdf"})
    assert again.json()["upload_id"] == uid
    # Checking before the details are complete is refused.
    assert client.post("/api/runs", json={"ipo_id": uid, "sample_mode": True}).status_code == 422
    assert client.patch(f"/api/uploads/{uid}", json={"issue_price": -5}).status_code == 422
    assert client.patch(f"/api/uploads/{uid}", json={"exchange": "LSE"}).status_code == 422
    done = client.patch(f"/api/uploads/{uid}", json={"issue_price": 120, "cin": "ignored"}).json()
    assert done["missing"] == [] and done["fields"]["issue_price"] == 120.0

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
