"""Automated tests for the Kisan Milan backend.

Run from the project folder:   python -m unittest discover -s tests -v
Uses a throw-away database; your real kisan_milan.db is never touched.
"""
import base64
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
_TMP = tempfile.mkdtemp()
os.environ["KISAN_MILAN_DB"] = os.path.join(_TMP, "test.db")

import app as km  # noqa: E402


def listing_payload(**kw):
    d = {"cropKey": "tomato", "cropName": "Tomato", "qty": 100, "condition": "ripe", "harvest": "today",
         "location": "Chevella", "price": 20}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        km.DB_PATH = os.path.join(_TMP, self.id().split(".")[-1] + ".db")
        if os.path.exists(km.DB_PATH):
            os.remove(km.DB_PATH)
        km.init_db()
        self.c = km.app.test_client()

    def make_listing(self, **kw):
        r = self.c.post("/api/listings", json=listing_payload(**kw))
        self.assertEqual(r.status_code, 201, r.data)
        return r.get_json()

    def make_offer(self, listing, qty, price=22):
        r = self.c.post("/api/offers", json={"listingId": listing["id"], "cropKey": listing["crop_key"],
                                              "qty": qty, "price": price, "buyerName": "B"})
        self.assertEqual(r.status_code, 201, r.data)
        return r.get_json()


class Security(Base):
    def test_source_and_database_are_not_served(self):
        for path in ("/app.py", "/kisan_milan.db", "/requirements.txt", "/README.md", "/tests/test_backend.py"):
            self.assertEqual(self.c.get(path).status_code, 404, path)

    def test_index_is_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Kisan Milan", r.data)


class Validation(Base):
    def test_nan_infinity_and_junk_rejected(self):
        for bad in (float("nan"), float("inf"), -5, 0, "abc", True, 1e12, [1]):
            r = self.c.post("/api/listings", json=listing_payload(qty=bad))
            self.assertEqual(r.status_code, 400, f"qty={bad!r} -> {r.status_code}")
        r = self.c.post("/api/listings", data='{"cropKey":"t","cropName":"T","qty":NaN,"condition":"r","harvest":"t","location":"x"}',
                        content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get("/api/listings").get_json(), [])

    def test_non_object_json_is_400_not_500(self):
        for path in ("/api/listings", "/api/offers", "/api/requirements", "/api/issues", "/api/quality-events"):
            self.assertEqual(self.c.post(path, json=[1, 2]).status_code, 400, path)
            self.assertEqual(self.c.post(path, data="not json", content_type="application/json").status_code, 400, path)

    def test_api_errors_are_json(self):
        r = self.c.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())
        self.assertEqual(self.c.delete("/api/listings").status_code, 405)
        self.assertIn("error", self.c.delete("/api/listings").get_json())

    def test_legacy_infinity_in_db_does_not_break_json(self):
        listing = self.make_listing()
        con = sqlite3.connect(km.DB_PATH)
        con.execute("UPDATE listings SET quantity_kg = 1e999 WHERE id=?", (listing["id"],))
        con.commit(); con.close()
        r = self.c.get("/api/listings")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Infinity", r.data)

    def test_offer_must_match_listing(self):
        l = self.make_listing(price=30)
        # A buyer's quoted price is allowed to differ from the farmer's asking price.
        quoted = self.c.post("/api/offers", json={"listingId": l["id"], "cropKey": "tomato", "qty": 10, "price": 22, "buyerName": "Buyer"})
        self.assertEqual(quoted.status_code, 201, quoted.data)
        self.assertEqual(quoted.get_json()["price_per_kg"], 22)
        self.assertEqual(self.c.post("/api/offers", json={"listingId": l["id"], "cropKey": "onion", "qty": 1, "price": 1}).status_code, 400)
        self.assertEqual(self.c.post("/api/offers", json={"listingId": l["id"], "cropKey": "tomato", "qty": 101, "price": 1}).status_code, 400)
        self.assertEqual(self.c.post("/api/offers", json={"listingId": "KM-NOPE", "cropKey": "tomato", "qty": 1, "price": 1}).status_code, 404)


class Authentication(Base):
    def test_buyer_registration_and_login(self):
        payload = {"name": "Buyer One", "email": "Buyer@example.com", "password": "testpass123", "role": "buyer"}
        created = self.c.post("/api/auth/register", json=payload)
        self.assertEqual(created.status_code, 201, created.data)
        logged_in = self.c.post("/api/auth/login", json={"email": "buyer@EXAMPLE.com", "password": "testpass123", "role": "buyer"})
        self.assertEqual(logged_in.status_code, 200, logged_in.data)
        self.assertEqual(logged_in.get_json()["user"]["role"], "buyer")
        role_recovery = self.c.post("/api/auth/login", json={"email": "buyer@example.com", "password": "testpass123", "role": "farmer"})
        self.assertEqual(role_recovery.status_code, 200, role_recovery.data)
        self.assertEqual(role_recovery.get_json()["user"]["role"], "farmer")
        self.assertTrue(role_recovery.get_json()["roleCorrected"])

    def test_full_lifecycle(self):
        l = self.make_listing(qty=300)
        o = self.make_offer(l, 100)
        r = self.c.patch("/api/offers/" + o["id"], json={"status": "Accepted"})
        self.assertEqual(r.status_code, 200)
        order = r.get_json()["order"]
        self.assertEqual(self.c.get("/api/listings/" + l["id"]).get_json()["quantity_kg"], 200)
        for st in ("Route Started", "Pickup Complete", "Load Confirmed", "Delivered"):
            self.assertEqual(self.c.patch("/api/orders/" + order["id"], json={"status": st}).get_json()["status"], st)
        # cannot skip / reopen
        self.assertEqual(self.c.patch("/api/orders/" + order["id"], json={"status": "Confirmed"}).status_code, 409)
        # accepting twice does not double-reserve
        again = self.c.patch("/api/offers/" + o["id"], json={"status": "Accepted"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.c.get("/api/listings/" + l["id"]).get_json()["quantity_kg"], 200)
        self.assertEqual(len(self.c.get("/api/orders").get_json()), 1)

    def test_accepting_the_whole_lot_marks_it_sold(self):
        """Original build returned HTTP 500 here (CHECK(quantity_kg > 0))."""
        l = self.make_listing(qty=50)
        o = self.make_offer(l, 50)
        r = self.c.patch("/api/offers/" + o["id"], json={"status": "Accepted"})
        self.assertEqual(r.status_code, 200, r.data)
        got = self.c.get("/api/listings/" + l["id"]).get_json()
        self.assertEqual((got["quantity_kg"], got["status"]), (0, "Sold"))
        self.assertEqual(self.c.get("/api/listings").get_json(), [])  # sold lots leave the live feed

    def test_cancelling_an_order_returns_stock(self):
        """Original build lost the reserved quantity forever."""
        l = self.make_listing(qty=100)
        o = self.make_offer(l, 100)
        order = self.c.patch("/api/offers/" + o["id"], json={"status": "Accepted"}).get_json()["order"]
        self.assertEqual(self.c.get("/api/listings/" + l["id"]).get_json()["status"], "Sold")
        self.assertEqual(self.c.patch("/api/orders/" + order["id"], json={"status": "Cancelled"}).status_code, 200)
        got = self.c.get("/api/listings/" + l["id"]).get_json()
        self.assertEqual((got["quantity_kg"], got["status"]), (100, "Listed"))
        # repeating the cancel must not add the stock twice
        self.assertEqual(self.c.patch("/api/orders/" + order["id"], json={"status": "Cancelled"}).status_code, 200)
        self.assertEqual(self.c.get("/api/listings/" + l["id"]).get_json()["quantity_kg"], 100)

    def test_overselling_is_blocked(self):
        l = self.make_listing(qty=100)
        o1, o2 = self.make_offer(l, 80), self.make_offer(l, 80)
        self.assertEqual(self.c.patch("/api/offers/" + o1["id"], json={"status": "Accepted"}).status_code, 200)
        self.assertEqual(self.c.patch("/api/offers/" + o2["id"], json={"status": "Accepted"}).status_code, 409)

    def test_reject_and_cancel_offer(self):
        l = self.make_listing()
        o = self.make_offer(l, 10)
        self.assertEqual(self.c.patch("/api/offers/" + o["id"], json={"status": "Rejected"}).status_code, 200)
        self.assertEqual(self.c.patch("/api/offers/" + o["id"], json={"status": "Accepted"}).status_code, 409)
        self.assertEqual(self.c.get("/api/orders").get_json(), [])

    def test_requirements_issues_quality_stats(self):
        self.assertEqual(self.c.post("/api/requirements", json={"buyerName": "ABC", "cropKey": "tomato", "qty": 250}).status_code, 201)
        self.assertEqual(self.c.post("/api/requirements", json={"buyerName": "", "cropKey": "tomato", "qty": 250}).status_code, 400)
        self.assertEqual(self.c.post("/api/issues", json={"reporterName": "R", "issueType": "delay"}).status_code, 201)
        self.assertEqual(self.c.post("/api/quality-events", json={"farmerKey": "ramesh", "cropKey": "tomato", "action": "verified", "grade": "A+"}).status_code, 201)
        self.assertEqual(self.c.post("/api/quality-events", json={"farmerKey": "ramesh", "cropKey": "tomato", "action": "verified", "grade": "Z"}).status_code, 400)
        s = self.c.get("/api/stats").get_json()
        self.assertEqual((s["requirements"], s["issues"], s["quality_events"]), (1, 1, 1))


def photo_fields(age_seconds=0, source="live-camera", raw=None):
    raw = raw if raw is not None else b"\xff\xd8\xff\xe0" + os.urandom(3000)
    taken = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {"photo": "data:image/jpeg;base64," + base64.b64encode(raw).decode(),
            "photoSource": source, "photoTakenAt": taken.isoformat()}


class MarketOptions(Base):
    def test_four_comparison_cards_with_correct_net(self):
        data = self.c.get("/api/market-options?crop=tomato&qty=500").get_json()
        opts = {o["id"]: o for o in data["options"]}
        self.assertEqual(list(opts), ["mandi", "restaurant", "hotel", "retailer"])  # mandi, restaurant, hotel, retailer
        self.assertEqual(opts["mandi"]["net"], 16.7)        # 18 - 0.5 - 0.8   (matches the screenshots)
        self.assertEqual(opts["restaurant"]["net"], 19.5)   # 22 - 2 - 0.5
        self.assertEqual(sum(1 for o in opts.values() if o["best"]), 1)
        self.assertTrue(opts["restaurant"]["best"] or opts["retailer"]["best"] or opts["hotel"]["best"])
        self.assertEqual(opts["restaurant"]["total_net"], 9750.0)
        self.assertTrue(data["demo"])

    def test_other_crops_and_bad_qty(self):
        onion = self.c.get("/api/market-options?crop=onion").get_json()["options"]
        tomato = self.c.get("/api/market-options?crop=tomato").get_json()["options"]
        self.assertNotEqual(onion[0]["price"], tomato[0]["price"])
        self.assertEqual(len(self.c.get("/api/market-options?crop=unknownthing").get_json()["options"]), 4)
        for bad in ("abc", "-4", "NaN", "inf"):
            self.assertEqual(self.c.get("/api/market-options?qty=" + bad).status_code, 400, bad)

    def test_accept_creates_order_and_sells_listing(self):
        l = self.make_listing(qty=500)
        r = self.c.post("/api/market-options/accept", json={"listingId": l["id"], "optionId": "restaurant", "price": 999})
        self.assertEqual(r.status_code, 201, r.data)
        got = r.get_json()
        self.assertEqual(got["offer"]["price_per_kg"], 22.0)          # price comes from the server, not the request
        self.assertEqual(got["total_net"], 9750.0)
        orders = self.c.get("/api/orders").get_json()
        self.assertEqual((orders[0]["buyer_name"], orders[0]["status"], orders[0]["quantity_kg"]), ("ABC Restaurant", "Confirmed", 500))
        self.assertEqual(self.c.get("/api/listings/" + l["id"]).get_json()["status"], "Sold")
        again = self.c.post("/api/market-options/accept", json={"listingId": l["id"], "optionId": "hotel"})
        self.assertEqual(again.status_code, 409)

    def test_accept_partial_and_errors(self):
        l = self.make_listing(qty=500)
        self.assertEqual(self.c.post("/api/market-options/accept", json={"listingId": l["id"], "optionId": "hotel", "qty": 200}).status_code, 201)
        left = self.c.get("/api/listings/" + l["id"]).get_json()
        self.assertEqual((left["quantity_kg"], left["status"]), (300, "Listed"))
        self.assertEqual(self.c.post("/api/market-options/accept", json={"listingId": l["id"], "optionId": "hotel", "qty": 301}).status_code, 409)
        self.assertEqual(self.c.post("/api/market-options/accept", json={"listingId": l["id"], "optionId": "nope"}).status_code, 404)
        self.assertEqual(self.c.post("/api/market-options/accept", json={"listingId": "KM-NOPE", "optionId": "hotel"}).status_code, 404)
        self.assertEqual(self.c.post("/api/market-options/accept", json={"optionId": "hotel"}).status_code, 400)


class IncomingOffers(Base):
    def test_pending_filter_shows_new_buyer_offers(self):
        l = self.make_listing()
        o1, o2 = self.make_offer(l, 10), self.make_offer(l, 20)
        self.c.patch("/api/offers/" + o1["id"], json={"status": "Rejected"})
        pending = self.c.get("/api/offers?status=Pending").get_json()
        self.assertEqual([o["id"] for o in pending], [o2["id"]])
        self.assertEqual(len(self.c.get("/api/offers").get_json()), 2)
        self.assertEqual(self.c.get("/api/offers?status=Bogus").status_code, 400)


class CameraOnlyPhotos(Base):
    def test_live_camera_photo_is_stored_and_served(self):
        r = self.c.post("/api/listings", json={**listing_payload(), **photo_fields()})
        self.assertEqual(r.status_code, 201, r.data)
        lid = r.get_json()["id"]
        self.assertTrue(r.get_json()["has_photo"])
        pic = self.c.get(f"/api/listings/{lid}/photo")
        self.assertEqual((pic.status_code, pic.mimetype), (200, "image/jpeg"))
        self.assertTrue(pic.data.startswith(b"\xff\xd8\xff"))
        meta = self.c.get("/api/listings/" + lid).get_json()
        self.assertEqual((meta["has_photo"], meta["photo_source"]), (True, "live-camera"))
        self.assertTrue(self.c.get("/api/listings").get_json()[0]["has_photo"])
        self.assertNotIn("data", meta)  # the image bytes are never inlined into JSON

    def test_order_includes_crop_details_and_photo(self):
        listing = self.c.post("/api/listings", json={**listing_payload(), **photo_fields()}).get_json()
        offer = self.make_offer(listing, 25, price=23)
        accepted = self.c.patch("/api/offers/" + offer["id"], json={"status": "Accepted"})
        self.assertEqual(accepted.status_code, 200, accepted.data)
        order = self.c.get("/api/orders").get_json()[0]
        self.assertEqual(order["listing_id"], listing["id"])
        self.assertEqual(order["crop_name"], "Tomato")
        self.assertEqual(order["crop_condition"], "ripe")
        self.assertEqual(order["crop_harvest"], "today")
        self.assertEqual(order["crop_location"], "Chevella")
        self.assertEqual(order["farmer_expected_price"], 20)
        self.assertTrue(order["has_photo"])
        self.assertEqual(order["photo_url"], f"/api/listings/{listing['id']}/photo")
        self.assertEqual(self.c.get(order["photo_url"]).status_code, 200)

    def test_farmer_sees_photo_on_pending_offer(self):
        listing = self.c.post("/api/listings", json={**listing_payload(), **photo_fields()}).get_json()
        self.c.post("/api/offers", json={"listingId": listing["id"], "cropKey": listing["crop_key"],
                                         "buyerName": "B", "qty": 10, "price": 20})
        offer = self.c.get("/api/offers?status=Pending").get_json()[0]
        self.assertTrue(offer["has_photo"])
        self.assertEqual(offer["photo_url"], f"/api/listings/{listing['id']}/photo")
        self.assertEqual(offer["photo_source"], "live-camera")
        self.assertTrue(offer["photo_taken_at"])
        self.assertEqual(offer["crop_condition"], listing["condition"])
        self.assertEqual(offer["crop_location"], listing["location"])
        self.assertEqual(self.c.get(offer["photo_url"]).status_code, 200)

    def test_photo_is_optional(self):
        l = self.make_listing()
        self.assertFalse(self.c.get("/api/listings/" + l["id"]).get_json()["has_photo"])
        self.assertEqual(self.c.get(f"/api/listings/{l['id']}/photo").status_code, 404)

    def test_gallery_and_bad_photos_are_rejected(self):
        bad = [
            {**photo_fields(source="gallery")},
            {**photo_fields(source="")},
            {**{k: v for k, v in photo_fields().items() if k != "photoSource"}},
            {**photo_fields(), "photo": "data:image/png;base64," + base64.b64encode(os.urandom(3000)).decode()},
            {**photo_fields(), "photo": "data:image/jpeg;base64,@@@not-base64@@@"},
            {**photo_fields(raw=b"GIF89a" + os.urandom(3000))},          # not a JPEG
            {**photo_fields(raw=b"\xff\xd8\xff" + os.urandom(500_000))},  # too large
            {**photo_fields(age_seconds=3600)},                           # old photo
            {**photo_fields(age_seconds=-3600)},                          # from the future
            {**photo_fields(), "photoTakenAt": "yesterday"},
            {**{k: v for k, v in photo_fields().items() if k != "photoTakenAt"}},
        ]
        for i, extra in enumerate(bad):
            r = self.c.post("/api/listings", json={**listing_payload(), **extra})
            self.assertEqual(r.status_code, 400, f"case {i}: {r.data[:120]}")
        self.assertEqual(self.c.get("/api/listings").get_json(), [])  # nothing half-saved

    def test_camera_capture_fallback_source_is_allowed(self):
        r = self.c.post("/api/listings", json={**listing_payload(), **photo_fields(source="camera-capture")})
        self.assertEqual(r.status_code, 201)


class Concurrency(Base):
    def test_parallel_accepts_never_oversell(self):
        l = self.make_listing(qty=100)
        offers = [self.make_offer(l, 30) for _ in range(10)]
        results = []

        def accept(offer_id):
            r = km.app.test_client().patch("/api/offers/" + offer_id, json={"status": "Accepted"})
            results.append(r.status_code)

        threads = [threading.Thread(target=accept, args=(o["id"],)) for o in offers]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(set(results)), [200, 409], results)
        self.assertEqual(results.count(200), 3, results)  # 3 x 30 = 90 <= 100; a 4th would need 120
        left = self.c.get("/api/listings/" + l["id"]).get_json()["quantity_kg"]
        self.assertEqual(left, 10)
        self.assertEqual(len(self.c.get("/api/orders").get_json()), 3)


class Migration(unittest.TestCase):
    def test_old_database_is_upgraded_and_keeps_its_data(self):
        path = os.path.join(_TMP, "old.db")
        if os.path.exists(path):
            os.remove(path)
        con = sqlite3.connect(path)
        con.executescript("""
        CREATE TABLE listings (
            id TEXT PRIMARY KEY, crop_key TEXT NOT NULL, crop_name TEXT NOT NULL,
            quantity_kg REAL NOT NULL CHECK(quantity_kg > 0), condition TEXT NOT NULL,
            harvest TEXT NOT NULL, location TEXT NOT NULL, expected_price REAL,
            seller_type TEXT NOT NULL DEFAULT 'individual', fpo_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Listed', created_at TEXT NOT NULL);
        CREATE TABLE offers (
            id TEXT PRIMARY KEY, listing_id TEXT, buyer_name TEXT NOT NULL,
            crop_key TEXT NOT NULL, quantity_kg REAL NOT NULL CHECK(quantity_kg > 0),
            price_per_kg REAL NOT NULL CHECK(price_per_kg > 0), delivery_time TEXT NOT NULL DEFAULT '',
            delivery_location TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Pending', created_at TEXT NOT NULL,
            FOREIGN KEY(listing_id) REFERENCES listings(id) ON DELETE SET NULL);
        INSERT INTO listings VALUES ('KM-OLD','tomato','Tomato',50,'ripe','today','X',20,'individual','','Listed','2026-01-01');
        INSERT INTO offers VALUES ('OFF-OLD','KM-OLD','B','tomato',50,22,'','','Pending','2026-01-01');
        """)
        con.commit(); con.close()
        km.DB_PATH = path
        km.init_db()
        c = km.app.test_client()
        self.assertEqual(c.get("/api/listings/KM-OLD").get_json()["quantity_kg"], 50)
        self.assertEqual(c.get("/api/offers").get_json()[0]["listing_id"], "KM-OLD")  # link survived the rebuild
        r = c.patch("/api/offers/OFF-OLD", json={"status": "Accepted"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(c.get("/api/listings/KM-OLD").get_json()["status"], "Sold")


if __name__ == "__main__":
    unittest.main(verbosity=2)
