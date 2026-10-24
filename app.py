"""Kisan Milan backend (Flask + SQLite) - fixed prototype build.

Run:  python -m pip install -r requirements.txt && python app.py
Then open http://127.0.0.1:5000

Environment variables (all optional):
  PORT              port to listen on (default 5000)
  HOST              interface to bind (default 127.0.0.1; use 0.0.0.0 to test from a phone on the same Wi-Fi)
  KISAN_MILAN_DB    path of the SQLite database file

Email/password registration and login are implemented for this local prototype. Google OAuth, payment processing and HTTPS are not configured.
Do not expose it to the public internet as-is.
"""
from __future__ import annotations

import base64
import binascii
import logging
import math
import os
import sqlite3
import uuid
import re
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KISAN_MILAN_DB", os.path.join(BASE_DIR, "kisan_milan.db"))

# static_folder=None: ONLY index.html is served. (The original served every file in this
# folder, including app.py and the database, to anyone who asked.)
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MB request bodies are plenty for JSON
log = logging.getLogger("kisan_milan")

MAX_QTY_KG = 10_000_000
MAX_PRICE = 100_000

# Crop photos must come from the camera. The browser sends a small JPEG plus the moment it was taken.
ALLOWED_PHOTO_SOURCES = ("live-camera", "camera-capture")
MAX_PHOTO_BYTES = 400 * 1024
PHOTO_MAX_AGE_SECONDS = 15 * 60

# Demo reference offers for the Mandi Bhav comparison (tomato figures). Real mandi prices
# (Agmarknet) can replace these later; the app labels them as demo data.
MARKET_OPTIONS = [
    {"id": "mandi", "kind": "Mandi", "icon": "🏛️", "buyer": "Chevella Mandi", "price": 18.0, "transport": 0.5,
     "handling": 0.8, "distance": 9, "delivery": "Same-day auction", "fit": "Open auction · no pre-agreed buyer",
     "verified": False},
    {"id": "restaurant", "kind": "Restaurant", "icon": "🏪", "buyer": "ABC Restaurant", "price": 22.0, "transport": 2.0,
     "handling": 0.5, "distance": 18, "delivery": "Tomorrow", "fit": "Ripe produce · 500 kg requirement",
     "verified": True},
    {"id": "hotel", "kind": "Hotel", "icon": "🏨", "buyer": "Green Leaf Hotel", "price": 21.0, "transport": 1.5,
     "handling": 0.4, "distance": 14, "delivery": "Next morning", "fit": "Fresh ripe produce · daily kitchen demand",
     "verified": True},
    {"id": "retailer", "kind": "Retailer", "icon": "🛒", "buyer": "FreshMart Retail", "price": 21.5, "transport": 1.3,
     "handling": 0.25, "distance": 24, "delivery": "Within 24h", "fit": "Ripe / partially ripe · 800 kg requirement",
     "verified": True},
]
CROP_PRICE_FACTOR = {"tomato": 1.0, "onion": 1.2, "potato": 1.1, "greenChilli": 3.0, "chilli": 3.0,
                     "paddy": 1.2, "millets": 1.8, "pulses": 3.5}


# ---------------------------------------------------------------------------
# database helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)  # autocommit; we manage transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def db(write: bool = False):
    """Open a connection that is ALWAYS closed afterwards.

    write=True starts the transaction with BEGIN IMMEDIATE, so two requests can never read the
    same stock figure and both reserve it (the original could oversell a listing under load).
    """
    conn = connect_db()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if write:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, full_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('farmer','buyer')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY, crop_key TEXT NOT NULL, crop_name TEXT NOT NULL,
            quantity_kg REAL NOT NULL CHECK(quantity_kg >= 0), condition TEXT NOT NULL,
            harvest TEXT NOT NULL, location TEXT NOT NULL, expected_price REAL,
            seller_type TEXT NOT NULL DEFAULT 'individual', fpo_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Listed', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS requirements (
            id TEXT PRIMARY KEY, buyer_name TEXT NOT NULL, crop_key TEXT NOT NULL,
            quantity_kg REAL NOT NULL CHECK(quantity_kg > 0), target_price REAL,
            delivery_time TEXT NOT NULL DEFAULT '', delivery_location TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Open', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS offers (
            id TEXT PRIMARY KEY, listing_id TEXT, buyer_name TEXT NOT NULL,
            crop_key TEXT NOT NULL, quantity_kg REAL NOT NULL CHECK(quantity_kg > 0),
            price_per_kg REAL NOT NULL CHECK(price_per_kg > 0), delivery_time TEXT NOT NULL DEFAULT '',
            delivery_location TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Pending', created_at TEXT NOT NULL,
            FOREIGN KEY(listing_id) REFERENCES listings(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, offer_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'Confirmed',
            created_at TEXT NOT NULL, FOREIGN KEY(offer_id) REFERENCES offers(id)
        );
        CREATE TABLE IF NOT EXISTS issues (
            id TEXT PRIMARY KEY, reporter_name TEXT NOT NULL, order_id TEXT NOT NULL DEFAULT '',
            issue_type TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Open', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quality_events (
            id TEXT PRIMARY KEY, farmer_key TEXT NOT NULL, crop_key TEXT NOT NULL,
            action TEXT NOT NULL, grade TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Recorded', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS listing_photos (
            listing_id TEXT PRIMARY KEY, mime TEXT NOT NULL, data BLOB NOT NULL,
            source TEXT NOT NULL, taken_at TEXT NOT NULL, received_at TEXT NOT NULL,
            FOREIGN KEY(listing_id) REFERENCES listings(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_listings_crop ON listings(crop_key, status);
        CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
        CREATE INDEX IF NOT EXISTS idx_offers_listing ON offers(listing_id);
        """)
    migrate_db()


def migrate_db() -> None:
    """Upgrade databases created by the original build.

    The old `listings` table had CHECK(quantity_kg > 0), so accepting an offer for a listing's
    whole quantity (remaining = 0 -> "Sold") raised an IntegrityError and returned HTTP 500.
    SQLite cannot alter a CHECK constraint, so rebuild the table (documented 12-step procedure).
    """
    conn = connect_db()
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='listings'").fetchone()
        if not row or "CHECK(quantity_kg > 0)" not in (row["sql"] or ""):
            return
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            new_sql = row["sql"].replace("CHECK(quantity_kg > 0)", "CHECK(quantity_kg >= 0)")
            new_sql = new_sql.replace("CREATE TABLE listings", "CREATE TABLE listings_new", 1)
            new_sql = new_sql.replace('CREATE TABLE "listings"', "CREATE TABLE listings_new", 1)
            conn.execute(new_sql)
            conn.execute("INSERT INTO listings_new SELECT * FROM listings")
            conn.execute("DROP TABLE listings")
            conn.execute("ALTER TABLE listings_new RENAME TO listings")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_crop ON listings(crop_key, status)")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def row_dict(row):
    """sqlite Row -> dict. Non-finite floats (from databases written by older builds) become None,
    because NaN/Infinity are not valid JSON and would break the browser's response.json()."""
    if row is None:
        return None
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, float) and not math.isfinite(v):
            out[k] = None
    if "has_photo" in out:
        out["has_photo"] = bool(out["has_photo"])
    return out


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------
def error(message: str, code: int = 400):
    return jsonify({"error": message}), code


def body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def number(value, maximum: float, required: bool = True):
    """Parse a positive, finite number <= maximum. NaN/Infinity/booleans/garbage raise ValueError."""
    if value is None or value == "":
        if required:
            raise ValueError("missing number")
        return None
    if isinstance(value, bool):
        raise ValueError("not a number")
    x = float(value)
    if not math.isfinite(x) or x <= 0 or x > maximum:
        raise ValueError("number out of range")
    return x


def text(value, limit: int, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()[:limit]


# ---------------------------------------------------------------------------
# pages + generic handlers
# ---------------------------------------------------------------------------
@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.after_request
def add_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.errorhandler(HTTPException)
def http_error(exc):
    if request.path.startswith("/api/"):
        return error(exc.description or exc.name, exc.code or 500)
    return exc


@app.errorhandler(Exception)
def unexpected_error(exc):
    log.exception("Unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return error("Internal server error.", 500)
    return "Internal server error", 500



# ---------------------------------------------------------------------------
# Email/password authentication (prototype)
# ---------------------------------------------------------------------------
@app.post("/api/auth/register")
def auth_register():
    data = body()
    name = text(data.get("name"), 100)
    email = text(data.get("email"), 254).lower()
    password = data.get("password")
    role = text(data.get("role"), 20)
    if not name or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return error("Enter your name and a valid email address.")
    if not isinstance(password, str) or len(password) < 8:
        return error("Password must be at least 8 characters long.")
    if role not in ("farmer", "buyer"):
        return error("Choose Farmer or Buyer.")
    user_id = str(uuid.uuid4())
    try:
        with db(write=True) as conn:
            conn.execute("INSERT INTO users(id,full_name,email,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                         (user_id, name, email, generate_password_hash(password), role, now_iso()))
    except sqlite3.IntegrityError:
        return error("An account with this email already exists. Please sign in.", 409)
    return jsonify({"ok": True, "user": {"id": user_id, "name": name, "email": email, "role": role}}), 201

@app.post("/api/auth/login")
def auth_login():
    data = body()
    email = text(data.get("email"), 254).lower()
    password = data.get("password")
    role = text(data.get("role"), 20)
    if not email or not isinstance(password, str):
        return error("Enter your email and password.")
    with db() as conn:
        user = conn.execute("SELECT id,full_name,email,password_hash,role FROM users WHERE email=?", (email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return error("Incorrect email or password.", 401)
    # The password has been verified, so honour the role selected on the sign-in screen.
    # This repairs accounts that were accidentally registered under the other role and
    # avoids trapping a valid user in the wrong dashboard. Farmer and buyer emails remain
    # separate accounts; only this verified account's role is corrected.
    actual_role = user["role"]
    if role in ("farmer", "buyer") and actual_role != role:
        with db(write=True) as conn:
            conn.execute("UPDATE users SET role=? WHERE id=?", (role, user["id"]))
        actual_role = role
    return jsonify({"ok": True, "roleCorrected": actual_role != user["role"],
                    "user": {"id": user["id"], "name": user["full_name"], "email": user["email"], "role": actual_role}})

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "Kisan Milan API", "time": now_iso()})


class PhotoError(ValueError):
    pass


def read_photo(data: dict):
    """Validate a camera photo sent with a listing. Returns (jpeg_bytes, source, taken_at_iso) or None.

    The server cannot prove a photo is not a photo of a screen, but it refuses anything that is not
    declared as camera-captured, not a JPEG, too big, or not taken in the last few minutes.
    """
    photo = data.get("photo")
    if photo in (None, ""):
        return None
    source = text(data.get("photoSource"), 30)
    if source not in ALLOWED_PHOTO_SOURCES:
        raise PhotoError("Crop photos must be taken with the live camera. Gallery or file uploads are not accepted.")
    prefix = "data:image/jpeg;base64,"
    if not isinstance(photo, str) or not photo.startswith(prefix):
        raise PhotoError("The photo must be a JPEG taken by the camera.")
    try:
        raw = base64.b64decode(photo[len(prefix):], validate=True)
    except (binascii.Error, ValueError):
        raise PhotoError("The photo data is not valid.")
    if len(raw) < 1024 or len(raw) > MAX_PHOTO_BYTES or not raw.startswith(b"\xff\xd8\xff"):
        raise PhotoError("The photo is too large or is not a valid JPEG.")
    try:
        taken = datetime.fromisoformat(str(data.get("photoTakenAt")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise PhotoError("photoTakenAt (when the photo was taken) is required with a photo.")
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - taken).total_seconds()
    if age > PHOTO_MAX_AGE_SECONDS or age < -300:
        raise PhotoError("The photo must be taken just now (within 15 minutes). Please retake it.")
    return raw, source, taken.astimezone(timezone.utc).isoformat(timespec="seconds")


def build_market_options(crop_key: str, qty=None) -> list:
    """The (up to) four comparison cards for Mandi Bhav: mandi, restaurant, hotel, retailer."""
    factor = CROP_PRICE_FACTOR.get(crop_key, 1.0)
    out = []
    for o in MARKET_OPTIONS:
        price = round(o["price"] * factor, 2)
        item = {**o, "price": price, "net": round(price - o["transport"] - o["handling"], 2)}
        if qty:
            item["total_net"] = round(item["net"] * qty, 2)
            item["total_gross"] = round(price * qty, 2)
        out.append(item)
    best = max(i["net"] for i in out)
    for i in out:
        i["best"] = i["net"] == best
    return out


# ---------------------------------------------------------------------------
# listings
# ---------------------------------------------------------------------------
LISTING_SELECT = """SELECT l.*, (p.listing_id IS NOT NULL) AS has_photo, p.source AS photo_source,
        p.taken_at AS photo_taken_at
        FROM listings l LEFT JOIN listing_photos p ON p.listing_id = l.id"""
@app.get("/api/listings")
def list_listings():
    crop = request.args.get("crop")
    status = request.args.get("status", "Listed")
    sql = LISTING_SELECT + " WHERE l.status = ?"
    params = [status]
    if crop:
        sql += " AND l.crop_key = ?"
        params.append(crop)
    sql += " ORDER BY l.created_at DESC, l.rowid DESC LIMIT 500"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([row_dict(r) for r in rows])


@app.post("/api/listings")
def create_listing():
    data = body()
    required = ["cropKey", "cropName", "qty", "condition", "harvest", "location"]
    missing = [k for k in required if data.get(k) in (None, "")]
    if missing:
        return error("Missing required fields: " + ", ".join(missing))
    try:
        qty = number(data["qty"], MAX_QTY_KG)
        price = number(data.get("price"), MAX_PRICE, required=False)
    except (ValueError, TypeError, OverflowError):
        return error("Quantity and price must be positive numbers within a realistic range.")
    try:
        photo = read_photo(data)
    except PhotoError as exc:
        return error(str(exc))
    item = {
        "id": "KM-" + uuid.uuid4().hex[:10].upper(), "crop_key": text(data["cropKey"], 60),
        "crop_name": text(data["cropName"], 80), "quantity_kg": qty,
        "condition": text(data["condition"], 40), "harvest": text(data["harvest"], 40),
        "location": text(data["location"], 160), "expected_price": price,
        "seller_type": text(data.get("sellerType"), 30, "individual") or "individual",
        "fpo_name": text(data.get("fpoName"), 120), "status": "Listed", "created_at": now_iso(),
    }
    with db(write=True) as conn:
        conn.execute("""INSERT INTO listings
          (id,crop_key,crop_name,quantity_kg,condition,harvest,location,expected_price,seller_type,fpo_name,status,created_at)
          VALUES (:id,:crop_key,:crop_name,:quantity_kg,:condition,:harvest,:location,:expected_price,:seller_type,:fpo_name,:status,:created_at)""", item)
        if photo:
            conn.execute("""INSERT INTO listing_photos (listing_id,mime,data,source,taken_at,received_at)
                            VALUES (?,?,?,?,?,?)""", (item["id"], "image/jpeg", photo[0], photo[1], photo[2], now_iso()))
    return jsonify({**item, "has_photo": bool(photo), "photo_source": photo[1] if photo else None,
                    "photo_taken_at": photo[2] if photo else None}), 201


@app.get("/api/listings/<listing_id>")
def get_listing(listing_id):
    with db() as conn:
        row = conn.execute(LISTING_SELECT + " WHERE l.id=?", (listing_id,)).fetchone()
    return (jsonify(row_dict(row)), 200) if row else error("Listing not found.", 404)


@app.get("/api/listings/<listing_id>/photo")
def listing_photo(listing_id):
    with db() as conn:
        row = conn.execute("SELECT mime, data FROM listing_photos WHERE listing_id=?", (listing_id,)).fetchone()
    if not row:
        return error("This listing has no photo.", 404)
    resp = Response(row["data"], mimetype=row["mime"])
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# ---------------------------------------------------------------------------
# buyer requirements
# ---------------------------------------------------------------------------
@app.post("/api/requirements")
def create_requirement():
    data = body()
    try:
        buyer = text(data.get("buyerName"), 100)
        crop = text(data.get("cropKey"), 60)
        qty = number(data.get("qty"), MAX_QTY_KG)
        price = number(data.get("targetPrice"), MAX_PRICE, required=False)
        if not buyer or not crop:
            raise ValueError("missing")
    except (ValueError, TypeError, OverflowError):
        return error("buyerName, cropKey and a positive qty are required; targetPrice is optional.")
    item = {"id": "REQ-" + uuid.uuid4().hex[:10].upper(), "buyer_name": buyer, "crop_key": crop,
            "quantity_kg": qty, "target_price": price, "delivery_time": text(data.get("deliveryTime"), 100),
            "delivery_location": text(data.get("deliveryLocation"), 160), "notes": text(data.get("notes"), 500),
            "status": "Open", "created_at": now_iso()}
    with db(write=True) as conn:
        conn.execute("""INSERT INTO requirements (id,buyer_name,crop_key,quantity_kg,target_price,delivery_time,delivery_location,notes,status,created_at)
        VALUES (:id,:buyer_name,:crop_key,:quantity_kg,:target_price,:delivery_time,:delivery_location,:notes,:status,:created_at)""", item)
    return jsonify(item), 201


@app.get("/api/requirements")
def list_requirements():
    with db() as conn:
        rows = conn.execute("SELECT * FROM requirements ORDER BY created_at DESC, rowid DESC LIMIT 500").fetchall()
    return jsonify([row_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Mandi Bhav: compare up to 4 markets, and accept one
# ---------------------------------------------------------------------------
@app.get("/api/market-options")
def market_options():
    crop = text(request.args.get("crop", "tomato"), 60) or "tomato"
    try:
        qty = number(request.args.get("qty"), MAX_QTY_KG, required=False)
    except (ValueError, TypeError, OverflowError):
        return error("qty must be a positive number.")
    return jsonify({"crop": crop, "demo": True, "options": build_market_options(crop, qty)})


@app.post("/api/market-options/accept")
def accept_market_option():
    """Farmer picks one comparison card: creates an accepted offer + a confirmed order in one transaction.
    The price comes from the server's table, never from the browser."""
    data = body()
    listing_id, option_id = text(data.get("listingId"), 60), text(data.get("optionId"), 30)
    if not listing_id or not option_id:
        return error("listingId and optionId are required.")
    try:
        wanted = number(data.get("qty"), MAX_QTY_KG, required=False)
    except (ValueError, TypeError, OverflowError):
        return error("qty must be a positive number.")
    with db(write=True) as conn:
        listing = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not listing:
            return error("Listing not found.", 404)
        if listing["status"] != "Listed" or listing["quantity_kg"] <= 0:
            return error("This crop is no longer available - it may already be sold.", 409)
        option = next((o for o in build_market_options(listing["crop_key"]) if o["id"] == option_id), None)
        if not option:
            return error("Unknown market option.", 404)
        qty = wanted or listing["quantity_kg"]
        if qty > listing["quantity_kg"]:
            return error("Quantity exceeds the available crop quantity.", 409)
        remaining = max(0.0, round(listing["quantity_kg"] - qty, 6))
        offer = {"id": "OFF-" + uuid.uuid4().hex[:10].upper(), "listing_id": listing_id, "buyer_name": option["buyer"],
                 "crop_key": listing["crop_key"], "quantity_kg": qty, "price_per_kg": option["price"],
                 "delivery_time": option["delivery"], "delivery_location": option["buyer"],
                 "status": "Accepted", "created_at": now_iso()}
        conn.execute("""INSERT INTO offers (id,listing_id,buyer_name,crop_key,quantity_kg,price_per_kg,delivery_time,delivery_location,status,created_at)
        VALUES (:id,:listing_id,:buyer_name,:crop_key,:quantity_kg,:price_per_kg,:delivery_time,:delivery_location,:status,:created_at)""", offer)
        conn.execute("UPDATE listings SET quantity_kg=?, status=? WHERE id=?",
                     (remaining, "Listed" if remaining > 0 else "Sold", listing_id))
        order = {"id": "ORD-" + uuid.uuid4().hex[:10].upper(), "offer_id": offer["id"],
                 "status": "Confirmed", "created_at": now_iso()}
        conn.execute("INSERT INTO orders (id,offer_id,status,created_at) VALUES (:id,:offer_id,:status,:created_at)", order)
    return jsonify({"offer": offer, "order": order, "net_per_kg": option["net"],
                    "total_net": round(option["net"] * qty, 2)}), 201


# ---------------------------------------------------------------------------
# offers
# ---------------------------------------------------------------------------
@app.post("/api/offers")
def create_offer():
    data = body()
    try:
        buyer = text(data.get("buyerName"), 100) or "Demo Buyer"
        crop = text(data.get("cropKey", "tomato"), 60)
        qty = number(data.get("qty"), MAX_QTY_KG)
        price = number(data.get("price"), MAX_PRICE)
        if not crop:
            raise ValueError("missing crop")
    except (ValueError, TypeError, OverflowError):
        return error("A crop, positive quantity and positive price are required.")
    listing_id = data.get("listingId") or None
    item = {"id": "OFF-" + uuid.uuid4().hex[:10].upper(), "listing_id": listing_id, "buyer_name": buyer,
            "crop_key": crop, "quantity_kg": qty, "price_per_kg": price,
            "delivery_time": text(data.get("deliveryTime"), 100),
            "delivery_location": text(data.get("deliveryLocation"), 160),
            "status": "Pending", "created_at": now_iso()}
    with db(write=True) as conn:
        if listing_id:
            listing = conn.execute("SELECT id,crop_key,quantity_kg,status FROM listings WHERE id=?", (listing_id,)).fetchone()
            if not listing:
                return error("The selected crop listing does not exist.", 404)
            if listing["status"] != "Listed":
                return error("The selected crop listing is no longer available.", 409)
            if listing["crop_key"] != crop:
                return error("Offer crop does not match the selected listing.")
            if qty > listing["quantity_kg"]:
                return error("Offer quantity exceeds the available crop quantity.")
        conn.execute("""INSERT INTO offers (id,listing_id,buyer_name,crop_key,quantity_kg,price_per_kg,delivery_time,delivery_location,status,created_at)
        VALUES (:id,:listing_id,:buyer_name,:crop_key,:quantity_kg,:price_per_kg,:delivery_time,:delivery_location,:status,:created_at)""", item)
    return jsonify(item), 201


@app.get("/api/offers")
def list_offers():
    status = request.args.get("status")
    if status and status not in ("Pending", "Accepted", "Rejected", "Cancelled"):
        return error("status must be Pending, Accepted, Rejected or Cancelled.")
    sql, params = ("SELECT offers.*, listings.expected_price AS farmer_expected_price, listings.crop_name AS crop_name, "
                   "listings.condition AS crop_condition, listings.harvest AS crop_harvest, "
                   "listings.location AS crop_location, p.source AS photo_source, p.taken_at AS photo_taken_at, "
                   "(p.listing_id IS NOT NULL) AS has_photo, "
                   "CASE WHEN p.listing_id IS NOT NULL THEN '/api/listings/' || listings.id || '/photo' ELSE NULL END AS photo_url "
                   "FROM offers LEFT JOIN listings ON listings.id = offers.listing_id "
                   "LEFT JOIN listing_photos p ON p.listing_id = listings.id"), []
    if status:
        sql, params = sql + " WHERE offers.status = ?", [status]
    with db() as conn:
        rows = conn.execute(sql + " ORDER BY offers.created_at DESC, offers.rowid DESC LIMIT 500", params).fetchall()
    return jsonify([row_dict(r) for r in rows])


@app.patch("/api/offers/<offer_id>")
def update_offer(offer_id):
    status = body().get("status")
    if status not in ("Accepted", "Rejected", "Cancelled"):
        return error("status must be Accepted, Rejected, or Cancelled.")
    with db(write=True) as conn:  # one atomic transaction: check stock, reserve it, create the order
        offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not offer:
            return error("Offer not found.", 404)
        current = offer["status"]
        if current == status:  # idempotent repeat of the same decision
            existing = conn.execute("SELECT * FROM orders WHERE offer_id=?", (offer_id,)).fetchone()
            return jsonify({"offer": row_dict(offer), "order": row_dict(existing)})
        if current != "Pending":
            return error("Only pending offers can be accepted, rejected or cancelled.", 409)
        order = None
        if status == "Accepted":
            if offer["listing_id"]:
                listing = conn.execute("SELECT * FROM listings WHERE id=?", (offer["listing_id"],)).fetchone()
                if not listing or listing["status"] != "Listed":
                    return error("This crop listing is no longer available.", 409)
                if offer["quantity_kg"] > listing["quantity_kg"]:
                    return error("Offer quantity exceeds the available crop quantity.", 409)
                remaining = max(0.0, listing["quantity_kg"] - offer["quantity_kg"])
                conn.execute("UPDATE listings SET quantity_kg=?, status=? WHERE id=?",
                             (remaining, "Listed" if remaining > 0 else "Sold", listing["id"]))
            order = {"id": "ORD-" + uuid.uuid4().hex[:10].upper(), "offer_id": offer_id,
                     "status": "Confirmed", "created_at": now_iso()}
            conn.execute("INSERT INTO orders (id,offer_id,status,created_at) VALUES (:id,:offer_id,:status,:created_at)", order)
        conn.execute("UPDATE offers SET status=? WHERE id=?", (status, offer_id))
        updated = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    return jsonify({"offer": row_dict(updated), "order": order})


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------
ORDER_SELECT = """SELECT o.id,o.offer_id,o.status,o.created_at,
        f.buyer_name,f.crop_key,f.quantity_kg,f.price_per_kg,f.delivery_time,f.delivery_location,
        f.listing_id,l.crop_name,l.condition AS crop_condition,l.harvest AS crop_harvest,
        l.location AS crop_location,l.expected_price AS farmer_expected_price,
        (p.listing_id IS NOT NULL) AS has_photo, p.source AS photo_source, p.taken_at AS photo_taken_at,
        CASE WHEN p.listing_id IS NOT NULL THEN '/api/listings/' || l.id || '/photo' ELSE NULL END AS photo_url
        FROM orders o
        JOIN offers f ON f.id=o.offer_id
        LEFT JOIN listings l ON l.id=f.listing_id
        LEFT JOIN listing_photos p ON p.listing_id=l.id"""

ORDER_TRANSITIONS = {
    "Confirmed": ("Route Started", "Cancelled"),
    "Route Started": ("Pickup Complete", "Cancelled"),
    "Pickup Complete": ("Load Confirmed", "Cancelled"),
    "Load Confirmed": ("Delivered", "Cancelled"),
    "Delivered": (), "Cancelled": (),
}


@app.get("/api/orders")
def list_orders():
    with db() as conn:
        rows = conn.execute(ORDER_SELECT + " ORDER BY o.created_at DESC, o.rowid DESC LIMIT 500").fetchall()
    return jsonify([row_dict(r) for r in rows])


@app.patch("/api/orders/<order_id>")
def update_order(order_id):
    status = body().get("status")
    if status not in ORDER_TRANSITIONS:
        return error("status must be one of: " + ", ".join(ORDER_TRANSITIONS) + ".")
    with db(write=True) as conn:
        order = conn.execute("""SELECT o.id,o.status,f.listing_id,f.quantity_kg
                                FROM orders o JOIN offers f ON f.id=o.offer_id WHERE o.id=?""", (order_id,)).fetchone()
        if not order:
            return error("Order not found.", 404)
        if status != order["status"]:
            if status not in ORDER_TRANSITIONS.get(order["status"], ()):
                return error("Invalid order status transition from " + order["status"] + " to " + status + ".", 409)
            if status == "Cancelled" and order["listing_id"]:
                # give the reserved crop back to the listing (the original lost it forever)
                conn.execute("UPDATE listings SET quantity_kg = quantity_kg + ?, status='Listed' WHERE id=?",
                             (order["quantity_kg"], order["listing_id"]))
            conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        row = conn.execute(ORDER_SELECT + " WHERE o.id=?", (order_id,)).fetchone()
    return jsonify(row_dict(row))


# ---------------------------------------------------------------------------
# quality events, issues, stats
# ---------------------------------------------------------------------------
@app.get("/api/quality-events")
def list_quality_events():
    with db() as conn:
        rows = conn.execute("SELECT * FROM quality_events ORDER BY created_at DESC, rowid DESC LIMIT 500").fetchall()
    return jsonify([row_dict(r) for r in rows])


@app.post("/api/quality-events")
def create_quality_event():
    data = body()
    farmer, crop, action = text(data.get("farmerKey"), 60), text(data.get("cropKey"), 60), text(data.get("action"), 40)
    if not farmer or not crop or action not in ("sample-requested", "new-sample", "verified", "bulk-confirmed"):
        return error("farmerKey, cropKey and a supported quality action are required.")
    grade = text(data.get("grade"), 10)
    if action == "verified" and grade not in ("A++", "A+", "A"):
        return error("Grade must be A++, A+ or A.")
    item = {"id": "QEV-" + uuid.uuid4().hex[:10].upper(), "farmer_key": farmer, "crop_key": crop,
            "action": action, "grade": grade, "reason": text(data.get("reason"), 200),
            "status": "Recorded", "created_at": now_iso()}
    with db(write=True) as conn:
        conn.execute("INSERT INTO quality_events (id,farmer_key,crop_key,action,grade,reason,status,created_at) VALUES (:id,:farmer_key,:crop_key,:action,:grade,:reason,:status,:created_at)", item)
    return jsonify(item), 201


@app.post("/api/issues")
def create_issue():
    data = body()
    reporter, issue_type = text(data.get("reporterName"), 100), text(data.get("issueType"), 80)
    if not reporter or not issue_type:
        return error("reporterName and issueType are required.")
    item = {"id": "ISS-" + uuid.uuid4().hex[:10].upper(), "reporter_name": reporter,
            "order_id": text(data.get("orderId"), 60), "issue_type": issue_type,
            "details": text(data.get("details"), 1000), "status": "Open", "created_at": now_iso()}
    with db(write=True) as conn:
        conn.execute("INSERT INTO issues (id,reporter_name,order_id,issue_type,details,status,created_at) VALUES (:id,:reporter_name,:order_id,:issue_type,:details,:status,:created_at)", item)
    return jsonify(item), 201


@app.get("/api/issues")
def list_issues():
    with db() as conn:
        rows = conn.execute("SELECT * FROM issues ORDER BY created_at DESC, rowid DESC LIMIT 500").fetchall()
    return jsonify([row_dict(r) for r in rows])


@app.get("/api/stats")
def stats():
    with db() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("listings", "requirements", "offers", "orders", "issues", "quality_events")}
    return jsonify(counts)


# Create tables whenever the module is imported (python app.py, flask run, gunicorn, tests...).
# The original only did this under `python app.py`, so any other way of starting it crashed.
init_db()

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 10000))
    print(f"Kisan Milan server starting on port {port}")
    app.run(host=host, port=port, debug=False, threaded=True)
