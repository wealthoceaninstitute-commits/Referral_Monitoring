"""End-to-end test against real NJ exports.  Usage:
    REPORTS_DIR=/path/to/four/xlsx pytest -q
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


def test_full_flow(api):
    m = login(api, os.environ.get("MASTER_EMAIL", "master@example.com"), os.environ.get("MASTER_PASSWORD", "change-me"))
    files = [("files", (os.path.basename(p), open(p, "rb").read())) for p in sorted(glob.glob(DIR + "/*.xlsx"))]
    assert len(files) == 4
    res = api.post("/uploads", files=files, headers=m).json()
    assert all(r["ok"] for r in res), res
    by = {r["report_type"]: r for r in res}
    assert by["AUM"]["rows_new"] == 293 and by["PURCHASE"]["rows_new"] == 1434
    assert by["REJECTION"]["rows_new"] == 115 and by["REDEMPTION"]["rows_new"] == 20  # 28 rows, 8 listed twice

    # uploading again must not double count anything
    again = {r["report_type"]: r for r in api.post("/uploads", files=files, headers=m).json()}
    assert again["PURCHASE"]["rows_new"] == 0 and again["REJECTION"]["rows_new"] == 0 and again["REDEMPTION"]["rows_new"] == 0

    d = api.get("/dashboard", headers=m).json()
    assert d["period"] == "2026-10"  # "as on 14-11" = closing position for October's transactions
    assert round(d["totals"]["aum"]) == 66754308 and round(d["totals"]["invested"]) == 58948060  # report's own Total row
    assert d["totals"]["active_clients"] == 293 and d["totals"]["purchase"]["count"] + d["unmatched_transactions"] >= 1434

    # hierarchy: master -> admin -> RA
    a = api.post("/users", json={"name": "Admin One", "email": "a1@x.com", "password": "password1", "role": "ADMIN"}, headers=m)
    assert a.status_code == 201
    a2 = api.post("/users", json={"name": "Admin Two", "email": "a2@x.com", "password": "password1", "role": "ADMIN"}, headers=m)
    ah, ah2 = login(api, "a1@x.com", "password1"), login(api, "a2@x.com", "password1")
    assert api.post("/users", json={"name": "X", "email": "x@x.com", "password": "password1", "role": "ADMIN"}, headers=ah).status_code == 403
    ra = api.post("/users", json={"name": "RA One", "email": "r1@x.com", "password": "password1", "role": "RA"}, headers=ah).json()
    ra2 = api.post("/users", json={"name": "RA Two", "email": "r2@x.com", "password": "password1", "role": "RA"}, headers=ah2).json()
    rh = login(api, "r1@x.com", "password1")
    assert api.get("/users", headers=rh).status_code == 403 and api.post("/uploads", files=files, headers=rh).status_code == 403
    # admins may upload too; a repeat upload adds nothing and leaves the book unchanged
    au = api.post("/uploads", files=files, headers=ah)
    assert au.status_code == 200 and all(r["ok"] for r in au.json())
    assert sum(r["rows_new"] for r in au.json() if r["report_type"] != "AUM") == 0
    assert api.get("/uploads", headers=ah).status_code == 200
    assert round(api.get("/dashboard", headers=m).json()["totals"]["aum"]) == 66754308

    inv = api.get("/investors", headers=ah).json()["items"]
    assert len(inv) >= 293 and api.get("/investors", headers=rh).json()["items"] == []
    pick = [i["id"] for i in inv[:10]]
    assert api.post("/investors/assign", json={"investor_ids": pick, "ra_id": ra2["id"]}, headers=ah).status_code == 403
    assert api.post("/investors/assign", json={"investor_ids": pick, "ra_id": ra["id"]}, headers=ah).json()["updated"] == 10

    mine = api.get("/investors", headers=rh).json()["items"]
    assert sorted(i["id"] for i in mine) == sorted(pick)
    assert all(i["pan"] is None or "*" in i["pan"] for i in mine)
    # admin dashboard = everything the admin can see (own RAs' clients + unassigned pool)
    ad = api.get("/dashboard", headers=ah).json()["totals"]
    assert ad["clients"] == len(inv) and round(ad["aum"]) == 66754308
    team = api.get("/dashboard?ra_id=assigned", headers=ah).json()["totals"]
    assert team["clients"] == 10 and abs(team["aum"] - sum(i["aum_total"] for i in mine)) < 1
    rd = api.get("/dashboard", headers=rh).json()
    assert rd["totals"]["clients"] == 10 and abs(rd["totals"]["aum"] - sum(i["aum_total"] for i in mine)) < 1
    assert rd["by_ra"] == []
    other = next(i["id"] for i in inv if i["id"] not in pick)
    assert api.get(f"/investors/{other}", headers=rh).status_code == 404
    assert api.get(f"/investors/{pick[0]}", headers=rh).status_code == 200
    # admin two cannot see or steal admin one's clients
    assert pick[0] not in [i["id"] for i in api.get("/investors", headers=ah2).json()["items"]]
    assert api.post("/investors/assign", json={"investor_ids": pick[:1], "ra_id": ra2["id"]}, headers=ah2).status_code == 403
    # master drills into an RA
    assert len(api.get(f"/investors?ra_id={ra['id']}", headers=m).json()["items"]) == 10
    t = api.get(f"/transactions?kind=purchase&ra_id={ra['id']}", headers=m).json()
    assert all(x["investor_id"] in pick for x in t["items"])
