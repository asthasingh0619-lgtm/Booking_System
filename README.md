# SeatBook – Movie Ticket Booking System

A full-stack movie ticket booking web app. Pick a movie, a show time and your seats, add food, pay online and get your tickets by email.

**Live demo:** _add your Render link here_

## Features
- Signup / login (PBKDF2 password hashing, token auth)
- Movie poster gallery, show times, live seat map (free / taken / yours / held)
- Group bookings in one row (all-or-nothing) and food ordering
- Online payments: Razorpay (UPI, cards, net banking) plus a practice **demo mode** when no keys are set
- Server-side price calculation, 5-minute seat holds, signature verification, webhook, automatic refund on cancel
- Waiting line for seats: if someone else is paying for a seat, the next person queues; when the first cancels or times out, the next person's payment window opens automatically
- Confirmation email, then a review email 2 minutes later; ratings and reviews under each movie
- 31 built-in unit tests

## Tech stack
Python, FastAPI, SQLAlchemy (SQLite), vanilla JavaScript (single `index.html`), smtplib, Razorpay

## Project files
| File | Purpose |
|---|---|
| `booking.py` | Backend: database, booking and payment rules, API, emails, tests |
| `index.html` | Frontend page served by the backend |
| `posters/` | Optional real poster images |
| `requirements.txt` | Python dependencies |

## Run locally
```bash
python -m venv venv
venv\Scripts\activate          # Windows   (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
python -m uvicorn booking:app --reload
```
Open http://127.0.0.1:8000

Run the tests:
```bash
python booking.py --test
```

## Deploy on Render
1. Push this project to GitHub (do **not** commit `.db` files, `venv/` or any secret keys).
2. Go to [render.com](https://render.com), sign in with GitHub and choose **New → Web Service**.
3. Select this repository and fill in:

   | Setting | Value |
   |---|---|
   | Language | Python 3 |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn booking:app --host 0.0.0.0 --port $PORT` |
   | Instance type | Free (to start) |

4. Open **Environment** and add the variables below.
5. Click **Create Web Service**. After the build finishes, open the URL Render gives you (`https://your-app.onrender.com`).

### Environment variables
| Variable | Needed? | Meaning |
|---|---|---|
| `APP_URL` | Yes | Your Render URL, so the link in review emails works |
| `RAZORPAY_KEY_ID` | For real payments | Razorpay Key Id (start with Test keys `rzp_test_...`) |
| `RAZORPAY_KEY_SECRET` | For real payments | Razorpay Key Secret. Keep it private |
| `RAZORPAY_WEBHOOK_SECRET` | Optional | Secret for `/api/razorpay/webhook` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | For real emails | e.g. Gmail: `smtp.gmail.com`, `587`, your address, an App Password. Without them emails are printed in the Render logs |
| `MAIL_FROM` | Optional | Sender address (defaults to `SMTP_USER`) |
| `BOOKING_DB` | Optional | Database file path (see the note below) |
| `PAYMENT_HOLD_SECONDS` | Optional | Seat hold time, default 300 |

No Razorpay keys means the app runs in **demo payment mode** (no real money).

### Razorpay webhook (optional)
In the Razorpay Dashboard → Webhooks, add `https://your-app.onrender.com/api/razorpay/webhook`, select `payment.captured` and `order.paid`, set a secret, and put the same value in `RAZORPAY_WEBHOOK_SECRET`.

### Important notes for Render
- **The free plan has no permanent disk.** The SQLite file is wiped whenever the service restarts or redeploys, so bookings and accounts disappear. For a demo this is fine. For lasting data, attach a Render Disk (paid) and set `BOOKING_DB=/var/data/booking_app.db`, or move to PostgreSQL.
- **Free services sleep** after about 15 minutes without visitors, so the first request can take around 30–60 seconds. Pending review emails are saved in the database, so they are sent after it wakes up.
- Login sessions are kept in memory, so everyone is logged out after a restart.
- Run a single instance only (the background worker and sessions are per process).

## How the booking flow works
1. **Checkout:** the server validates the request, calculates the price and holds the seats (status `pending`).
2. **Payment:** the customer pays in Razorpay's window (or the demo window).
3. **Verify:** the server checks the HMAC-SHA256 signature (or receives the webhook) and marks the booking `paid`. Only then are tickets confirmed and emails sent.
4. **Expiry / cancel:** unpaid holds are released after 5 minutes; cancelling a paid booking refunds it.

A database unique constraint on (show, seat) guarantees one seat can never be sold twice.

## API docs
FastAPI provides interactive docs at `/docs`.

## License
Free to use for learning.
