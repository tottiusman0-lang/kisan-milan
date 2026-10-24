# 🌾 KISAN MILAN
### Agriculture Market Platform

🚀 **Live Working Prototype:** https://kisan-milan.onrender.com

💻 **Source Code:** Available in this repository.

Kisan Milan connects farmers, FPOs, local vendors,
retailers, restaurants, and other buyers.

## Key Features
- Farmer crop listings
- Buyer access to available produce
- Crop quantity and quality information
- Order and logistics coordination
- Improved agricultural market access
# Kisan Milan — Python backend MVP (fixed build)

This project serves the existing Kisan Milan HTML app from Flask and stores shared workflow records in a local SQLite database.

## Run locally

Requires Python 3.10 or newer and an internet connection only if Flask must be installed for the first time.

### Windows

1. Extract this ZIP.
2. Open the extracted `kisan_milan_backend` folder.
3. Double-click `run_windows.bat`, or open Command Prompt in this folder and run:

   ```bat
   py -m pip install -r requirements.txt
   py app.py
   ```

4. Open **http://127.0.0.1:5000** in your browser. Do not open `index.html` directly from File Manager; the page needs the Python server.

### macOS / Linux

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Then open **http://127.0.0.1:5000**.

The SQLite database is created as `kisan_milan.db` in this folder. Keep the database file if you want to keep your data. Delete it only if you intentionally want to reset the local prototype data.

## New in this version

**1. Crop photos: live camera only (no gallery).**
- The "Upload photo" buttons are now **Take live photo** buttons. They open the camera inside the page. There is no gallery or file picker.
- If the browser cannot show a live preview (this happens on `http://192.168.x.x` addresses), the phone's camera app opens instead, and any photo older than 2 minutes is refused.
- The server accepts a photo only if it is marked as camera-captured, is a small JPEG, and was taken within the last 15 minutes. It stores the photo, the capture time and the source. Buyers see the photo with "Live camera photo · taken 6 min ago" on each listing.
- Honest limit: no web page can prove a picture is real. Someone could still photograph a screen, or use a virtual-camera app. This closes the easy route (picking a fake image from the gallery) and records evidence. Stronger checks (GPS and time watermark, random "show 3 fingers" challenge, AI-image detection) can be added later.

**2. Mandi Bhav: compare up to 4 offers.** It shows Mandi, Restaurant, Hotel and Retailer as cards (buyer offer - transport - handling = estimated net, route, and a "best net" badge). **Accept** creates an order from your latest crop listing, and **Details** shows the total you would receive. The prices are demo reference data until live Agmarknet prices are connected. They come from the server (`GET /api/market-options`), and the server, not the browser, decides the price when you accept.

**3. Orders: buyer offers appear for the farmer.** My Orders now has a "Buyer offers for you" card with Accept / Reject. A new offer triggers an alert ("New offer from ...") within about 6 seconds, and a number badge appears on the My Orders tab. Accepting an offer moves it into "Live orders".

**Open the app at http://127.0.0.1:5000, not by opening index.html as a file.** If the address bar shows `content://` or `file://`, the server features (orders, offers, photos, Mandi Bhav cards) cannot work.

**Live camera on a phone:** run the server in Termux and open `http://127.0.0.1:5000` in Chrome on the same phone. That address is treated as secure, so the live camera preview works there.

## What was fixed in this build

Found by running the original build in a real browser and through the API, then re-testing after the fixes.

| # | Problem in the original | Fix |
|---|---|---|
| 1 | **Security:** the server served every file in its folder, so anyone could download `app.py`, `kisan_milan.db` and the README from the browser. | Only `index.html` and the `/api/...` routes are served now. |
| 2 | **Crash on full sale:** accepting an offer for a listing's *entire* quantity returned HTTP 500 (the database rejected a quantity of 0). | Constraint fixed; the listing becomes `Sold`. Existing `kisan_milan.db` files are upgraded automatically on start, keeping all data. |
| 3 | **Data corruption:** `NaN` / `Infinity` / huge numbers were accepted (500 error, or `Infinity` stored, which then broke the buyer's live-listings screen for everyone). | Strict numeric validation with sane limits; bad values already in a database are hidden instead of breaking the page. |
| 4 | **Lost stock:** cancelling an order never returned the reserved crop to the listing. | Cancelling now returns the quantity (once, even if repeated). |
| 5 | **Overselling risk:** stock was checked and reserved in separate steps, so simultaneous accepts could both succeed. | Each decision is one atomic database transaction (`BEGIN IMMEDIATE`). |
| 6 | **Server only worked via `python app.py`:** tables were created only in `__main__`, so `flask run` / gunicorn failed with "no such table". | Tables are created on import. |
| 7 | **Leaked connections:** database connections were never closed (can keep `kisan_milan.db` locked on Windows). | Every connection is closed. |
| 8 | **Bad requests gave HTML 500 pages:** a JSON body that wasn't an object, unknown API paths, or wrong methods. | JSON errors with the right status (400 / 404 / 405). |
| 9 | **Driver screen updated the wrong order:** it always changed the newest order, even a delivered one. | Updates the oldest unfinished order; shows a clear message if none exists. |
| 10 | Lists had no limit; no way to open from a phone. | Lists capped at 500 rows; set `HOST=0.0.0.0` to test from a phone on the same Wi-Fi. |

## Run the tests

```
python -m unittest discover -s tests -v
```

24 tests cover security, validation, the full offer-to-delivery lifecycle, overselling (including 10 simultaneous accepts), cancellation, upgrading an old database, the Mandi Bhav cards and accepting them, incoming-offer filtering, and the camera-only photo rules. They use a temporary database, never your real one.

## Test from a phone on the same Wi-Fi

```
HOST=0.0.0.0 python3 app.py                      # Mac / Linux
$env:HOST="0.0.0.0"; py app.py                    # Windows PowerShell
```
Then open `http://<your-computer-IP>:5000` on the phone. There is still no login, so only do this on a trusted network.

## Connected workflows

- **Crop listing:** the farmer form validates and saves crop listings to SQLite.
- **Live listings:** buyer screen reads available listings from the backend.
- **Buyer offers:** offers are stored; offers linked to a live listing are checked against crop type and available quantity.
- **Offer decision:** farmer can accept/reject pending offers. Accepting a linked offer reserves the requested quantity and creates an order.
- **Buyer requirements:** save and view requirements from SQLite.
- **Orders and delivery statuses:** orders are created when offers are accepted. Status transitions are validated: Confirmed → Route Started → Pickup Complete → Load Confirmed → Delivered. Cancel is also supported through the API.
- **Quality actions:** sample requests, grade events, and bulk-confirmation events are recorded in SQLite. The prototype also uses browser local storage for the current display state.
- **Issue reporting:** issue submissions are stored with an ID and Open status.
- **Health check:** open `/api/health` to check whether the server is responding.

## Main API routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/health` | Server health |
| GET / POST | `/api/listings` | List or create crop listings |
| GET | `/api/listings/<id>` | Read one listing |
| GET / POST | `/api/requirements` | List or save buyer requirements |
| GET / POST | `/api/offers` | List or submit offers |
| PATCH | `/api/offers/<id>` | Accept, reject, or cancel a pending offer |
| GET | `/api/orders` | List orders |
| PATCH | `/api/orders/<id>` | Update an order's delivery status |
| GET / POST | `/api/issues` | List or record reported issues |
| GET / POST | `/api/quality-events` | List or record quality workflow events |
| GET | `/api/stats` | Counts of saved records |

## Important limitations — not production-ready yet

This is a working **local prototype**, not a live commercial marketplace. There is no real user login or role-based access control, so anyone who can access the local app can see and change the prototype's records. Do not expose it to the public internet as-is.

- Payment screen is only a demo; **no money is transferred**. A payment provider and secure payment verification are needed.
- WhatsApp messages, voice-to-listing, live GPS/ETA, real-time mandi prices, verified trader identity, and independent crop-quality certification are not connected to external services.
- Photo-based ripeness analysis is a browser-side heuristic, not an agricultural certification model; photos are not uploaded to the server in this version.
- The demo marketplace cards are sample data. Use the **Live farmer listings** panel to make offers on listings actually saved to the database.
- Buyer and farmer records are not separated by accounts yet. Authentication, authorization, validation hardening, backups, HTTPS, and deployment configuration are required before real users use this app.

## Database reset

Stop the server and delete `kisan_milan.db` to reset all local records. This is destructive; export anything important first.


## Crop details and photos in orders
The `GET /api/orders` response includes the linked farmer listing's crop name, quality/condition,
harvest timing, farm location, farmer expected price, `has_photo`, and `photo_url` when a listing
has a saved camera photo. The orders panels in `index.html` display these details and the photo.
Listings created without a photo remain supported.

## Farmer can now see the crop photo (fix)
The crop photo is shown to the farmer on **Buyer offers for you** (before accepting), on the dashboard
**Buyer offers** list, and on **Live orders**. Tap a photo to enlarge it. Cards also show the crop's
quality/condition, harvest and farm location. If a listing has no photo, a clear "No crop photo" note is shown.
API: `/api/offers` and `/api/orders` now return `photo_url`, `photo_source`, `photo_taken_at`, `crop_condition`, `crop_harvest`, `crop_location`.

## Improved crop image analysis (prototype)
The ripeness checker now includes basic image exposure checks, a larger analysis sample, conservative HSV colour classification, approximate central-region weighting, colour proportions, and a clearly labelled heuristic confidence estimate. This is still a browser-side heuristic, not a trained AI model or certified quality grade. The central-region weighting is not true crop segmentation. For a production-grade AI+HSV pipeline, collect and label crop-specific images, add a validated segmentation/classification model, and evaluate it on held-out images before presenting confidence as model probability.


## Fixes in this build

- **Use my location:** asks the phone/browser for its current GPS location, then tries to convert the coordinates into a village/town and district. It no longer inserts a fixed Chevella location. Allow location permission and turn on GPS. If address lookup is unavailable, coordinates are shown instead.
- **Buyer/Farmer login:** if a valid email and password are entered but that account was saved under the other role, signing in from the selected role corrects the account role and opens the selected dashboard. Use separate email accounts if you want separate Farmer and Buyer accounts.
- **Mandi language:** missing translations now fall back to English instead of leaving text in a previous language, and the chosen language is remembered when the page is refreshed. Telugu, Hindi and English can be selected from the language buttons.
