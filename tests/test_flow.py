"""End-to-end test against real NJ exports.  Usage:
    REPORTS_DIR=/path/to/the/xlsx/files pytest -q
The folder needs the four monthly reports plus the E-Wealth Reg. Status report.
Skipped when REPORTS_DIR is not set, so no client data needs to live in the repo."""
import glob
import os

import pytest

DIR = os.environ.get("REPORTS_DIR")
pytestmark = pytest.mark.skipif(not DIR, reason="REPORTS_DIR not set")


@pytest.fixture(scope="module")
def api():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def login(api, email, pw):
    r = api.post("/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def load(pattern):
    p = glob.glob(f"{DIR}/*{pattern}*.xlsx")
    assert len(p) == 1, (pattern, p)
    return ("files", (os.path.basename(p[0]), open(p[0], "rb").read()))


def test_full_flow(api):
    m = login(api, os.environ.get("MASTER_EMAIL", "master@example.com"), os.environ.get("MASTER_PASSWORD", "change-me"))
    aum, reg = load("InvestorWiseAUM"), load("RegStatus")
    files = [aum, load("ProcessedTransaction"), load("TransactionRejection"), load("MFRedemption")]
    res = api.post("/uploads", files=files, headers=m).json()
    assert all(r["ok"] for r in res), res
    by = {r["report_type"]: r for r in res}
    assert by["AUM"]["rows_new"] == 293 and by["AUM"]["as_on"] == "2026-11-14" and by["PURCHASE"]["rows_new"] == 1434
    assert by["REJECTION"]["rows_new"] == 115 and by["REDEMPTION"]["rows_new"] == 20  # 28 rows, 8 listed twice

    # uploading again must not double count anything, and the replaced AUM upload leaves history
    again = {r["report_type"]: r for r in api.post("/uploads", files=files, headers=m).json()}
    assert again["PURCHASE"]["rows_new"] == 0 and again["REJECTION"]["rows_new"] == 0 and again["REDEMPTION"]["rows_new"] == 0
    hist = api.get("/uploads", headers=m).json()
    assert [h["id"] for h in hist if h["report_type"] == "AUM"] == [again["AUM"]["id"]]

    d = api.get("/dashboard", headers=m).json()
    assert d["as_on"] == "2026-11-14" and d["period"] == "2026-10"  # latest AUM date; latest month with purchases
    assert round(d["totals"]["aum"]) == 66754308 and round(d["totals"]["invested"]) == 58948060  # report's own Total row
    assert d["totals"]["active_clients"] == 293 and d["totals"]["purchase"]["count"] + d["unmatched_transactions"] >= 1434
    base_clients = d["totals"]["clients"]

    # ---- a second AUM snapshot on another exact date sits beside the first ----
    older = api.post("/uploads", files=[aum], data={"as_on": "2026-10-01"}, headers=m).json()[0]
    assert older["ok"] and older["as_on"] == "2026-10-01"
    p = api.get("/periods", headers=m).json()
    assert p["aum_dates"] == ["2026-11-14", "2026-10-01"] and p["latest_aum"] == "2026-11-14"
    d2 = api.get("/dashboard?as_on=2026-10-01", headers=m).json()
    assert d2["as_on"] == "2026-10-01" and round(d2["totals"]["aum"]) == 66754308
    assert [h["as_on"] for h in d2["aum_history"]] == ["2026-11-14", "2026-10-01"]
    # ---- deleting an upload removes exactly what it added ----
    out = api.delete(f"/uploads/{older['id']}", headers=m).json()
    assert out["rows_removed"] == 293
    assert api.get("/periods", headers=m).json()["aum_dates"] == ["2026-11-14"]
    assert round(api.get("/dashboard", headers=m).json()["totals"]["aum"]) == 66754308

    # ---- registered client list ----
    r1 = api.post("/uploads", files=[reg], headers=m).json()[0]
    assert r1["ok"] and r1["report_type"] == "REGISTRATION" and r1["rows_new"] == 501, r1
    assert api.post("/uploads", files=[reg], headers=m).json()[0]["rows_new"] == 0
    d3 = api.get("/dashboard", headers=m).json()["totals"]
    assert d3["clients"] > base_clients and d3["active_clients"] == 293 and round(d3["aum"]) == 66754308
    assert d3["not_invested"] == d3["clients"] - 293
    allc = api.get("/investors", headers=m).json()["items"]
    ni = api.get("/investors?status=not_invested", headers=m).json()["items"]
    assert len(ni) == d3["not_invested"] and all(not i["active"] for i in ni)
    assert len(api.get("/investors?status=holding", headers=m).json()["items"]) == 293
    assert sum(1 for i in ni if i["registered"]) >= 195  # ~205 registered accounts have no holding
    holding = [i for i in allc if i["active"]]
    assert sum(1 for i in holding if i["pan"]) >= 289  # the list fills PANs for all but unresolved namesakes

    # same-name clients: giving one of them its PAN links every waiting transaction and registration
    dd = api.get("/dashboard", headers=m).json()
    assert dd["unmatched_transactions"] == 16 and dd["unmatched_registrations"] == 2
    orphan_pan = api.get("/transactions?unmatched=true", headers=m).json()["items"][0]["pan"]
    twin = next(i for i in allc if i["namesake"] and not i["pan"])
    assert api.patch(f"/investors/{twin['id']}", json={"pan": orphan_pan}, headers=m).json()["relinked"] == 18
    dd = api.get("/dashboard", headers=m).json()
    assert dd["unmatched_transactions"] == 0 and dd["unmatched_registrations"] == 0
    allc = api.get("/investors", headers=m).json()["items"]
    assert all(i["pan"] for i in allc if i["active"])
    holding = [i for i in allc if i["active"]]

    # hierarchy: master -> admin -> RA
    a = api.post("/users", json={"name": "Admin One", "email": "a1@x.com", "password": "password1", "role": "ADMIN"}, headers=m)
    assert a.status_code == 201
    api.post("/users", json={"name": "Admin Two", "email": "a2@x.com", "password": "password1", "role": "ADMIN"}, headers=m)
    ah, ah2 = login(api, "a1@x.com", "password1"), login(api, "a2@x.com", "password1")
    assert api.post("/users", json={"name": "X", "email": "x@x.com", "password": "password1", "role": "ADMIN"}, headers=ah).status_code == 403
    ra = api.post("/users", json={"name": "RA One", "email": "r1@x.com", "password": "password1", "role": "RA"}, headers=ah).json()
    ra2 = api.post("/users", json={"name": "RA Two", "email": "r2@x.com", "password": "password1", "role": "RA"}, headers=ah2).json()
    rh = login(api, "r1@x.com", "password1")
    assert api.get("/users", headers=rh).status_code == 403 and api.post("/uploads", files=files, headers=rh).status_code == 403
    assert api.delete(f"/uploads/{r1['id']}", headers=rh).status_code == 403
    # admins may upload; they can delete their own uploads but not the master's
    au = api.post("/uploads", files=[aum], data={"as_on": "2026-09-30"}, headers=ah).json()[0]
    assert au["ok"] and api.delete(f"/uploads/{r1['id']}", headers=ah).status_code == 403
    mine_hist = {h["id"]: h["can_delete"] for h in api.get("/uploads", headers=ah).json()}
    assert mine_hist[au["id"]] is True and mine_hist[r1["id"]] is False
    assert api.delete(f"/uploads/{au['id']}", headers=ah).status_code == 200

    inv = api.get("/investors", headers=ah).json()["items"]
    assert len(inv) == len(allc) and api.get("/investors", headers=rh).json()["items"] == []
    pick = [i["id"] for i in holding[:8]] + [i["id"] for i in ni[:2]]
    assert api.post("/investors/assign", json={"investor_ids": pick, "ra_id": ra2["id"]}, headers=ah).status_code == 403
    assert api.post("/investors/assign", json={"investor_ids": pick, "ra_id": ra["id"]}, headers=ah).json()["updated"] == 10

    mine = api.get("/investors", headers=rh).json()["items"]
    assert sorted(i["id"] for i in mine) == sorted(pick)
    assert all(i["pan"] is None or "*" in i["pan"] for i in mine)
    ad = api.get("/dashboard", headers=ah).json()
    assert ad["totals"]["clients"] == len(inv) and round(ad["totals"]["aum"]) == 66754308
    row = next(x for x in ad["by_ra"] if x["ra_id"] == ra["id"])
    assert row["clients"] == 10 and row["holding"] == 8 and row["not_invested"] == 2
    team = api.get("/dashboard?ra_id=assigned", headers=ah).json()["totals"]
    assert team["clients"] == 10 and abs(team["aum"] - sum(i["aum_total"] for i in mine)) < 1
    rd = api.get("/dashboard", headers=rh).json()
    assert rd["totals"]["clients"] == 10 and rd["totals"]["not_invested"] == 2 and rd["by_ra"] == []
    assert abs(rd["totals"]["aum"] - sum(i["aum_total"] for i in mine)) < 1
    other = next(i["id"] for i in inv if i["id"] not in pick)
    assert api.get(f"/investors/{other}", headers=rh).status_code == 404
    det = api.get(f"/investors/{pick[0]}", headers=rh).json()
    assert [s["as_on"] for s in det["snapshots"]] == ["2026-11-14"] and det["registrations"]
    # admin two cannot see or steal admin one's clients
    assert pick[0] not in [i["id"] for i in api.get("/investors", headers=ah2).json()["items"]]
    assert api.post("/investors/assign", json={"investor_ids": pick[:1], "ra_id": ra2["id"]}, headers=ah2).status_code == 403
    # master drills into an RA
    assert len(api.get(f"/investors?ra_id={ra['id']}", headers=m).json()["items"]) == 10
    t = api.get(f"/transactions?kind=purchase&ra_id={ra['id']}", headers=m).json()
    assert all(x["investor_id"] in pick for x in t["items"])

    # deleting the registration list removes the never-invested clients, except those given to an agent
    out = api.delete(f"/uploads/{r1['id']}", headers=m).json()
    assert out["rows_removed"] == 501 and out["clients_removed"] > 150
    left = api.get("/investors", headers=m).json()["items"]
    assert len(left) == base_clients + 2 and round(api.get("/dashboard", headers=m).json()["totals"]["aum"]) == 66754308
    # deleting the processed report removes its purchases only
    out = api.delete(f"/uploads/{by['PURCHASE']['id']}", headers=m).json()
    assert out["rows_removed"] == 1434
    assert api.get("/transactions?kind=purchase", headers=m).json()["total"] == 0
    assert api.get("/transactions?kind=rejection", headers=m).json()["total"] > 0
