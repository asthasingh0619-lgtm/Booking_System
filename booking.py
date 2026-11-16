"""
SeatBook BACKEND: MOVIE ticket booking with sign-up / login, group booking and no double booking.

Two files make the whole app (keep them in the same folder):
    booking.py   - this file: database, booking rules, login, e-mail and the /api/... URLs
    index.html   - the web page (design + JavaScript) that the browser shows

Run:  python -m uvicorn booking:app --reload      then open http://127.0.0.1:8000/
Test: python booking.py --test        (built-in tests, nothing extra to install)

How a user books (the page follows these steps):
    1. choose a MOVIE  ->  2. choose a SHOW TIME  ->  3. the seat map of that show appears
    ->  click a seat  ->  pop-up asks how many people and each person's name  ->  Confirm.
- The clicked seat and the next seats to its right (same row) are booked together
  under ONE booking reference. All seats are booked or none are.
- Every ticket is one row guarded by UNIQUE(show_id, seat), so the database
  decides the winner when several users race for the same seat.
- Passwords are stored only as salted PBKDF2-SHA256 hashes (standard library).
- After a booking, a confirmation email (movie, show time, seats + names) is sent to the
  user's registered email using Python's built-in smtplib. Set SMTP_HOST, SMTP_PORT,
  SMTP_USER and SMTP_PASSWORD in the terminal first. Without them, the email text is
  printed in the server terminal.

FOOD: in the same booking pop-up the user can add food and drinks (popcorn, cold drink, ...).
The food order is saved together with the tickets (same transaction), is shown in the
booking details and the confirmation email, and is cancelled together with the tickets.
There is no online payment in this app: it only records and totals the order.

PAYMENTS: pay online before the tickets are confirmed. The server works out the price itself (ticket price from the
MOVIES list x number of people + food), holds the chosen seats for PAYMENT_HOLD_SECONDS (default 5 minutes) while
the customer pays, and confirms the booking only after the payment is verified. Unpaid holds free the seats again.
  - Real payments use Razorpay (UPI, cards, net banking, wallets - INR). Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
    (use the Test Mode keys first). The customer pays inside Razorpay's own window, so card details never reach this
    server. Optional: RAZORPAY_WEBHOOK_SECRET + a public URL to /api/razorpay/webhook, so a payment is still confirmed
    if the customer closes the browser right after paying.
  - Without keys the app runs in DEMO mode: a practice payment window, no real money moves.
  - Cancelling a paid booking refunds it in full.
  - QUEUE: if someone else is paying for a seat right now, the next person joins a waiting line for it (first come,
    first served). The first person gets the payment window. If they cancel or run out of time, the next person's
    payment window opens by itself; if they pay, the others are told the seat is gone.

REVIEWS: 2 minutes (REVIEW_DELAY_SECONDS) after the ticket email, a second email asks the user to review the movie.
Its link opens the site with the review box ready. Only people who booked a movie can review it (one review per
person, they can edit it later). Reviews are shown under the movie when it is selected, and the average star rating
is shown on its poster. The review email is sent by a small background thread that starts with the server; unsent
review emails are kept in the database, so a server restart does not lose them.
Set APP_URL (default http://127.0.0.1:8000) to the address of your site so the link in the email opens correctly.

POSTERS: the home page has a poster slider (gallery) of all movies. Posters are drawn automatically; to use a
real poster image put it in the "posters" folder next to this file and set "poster": "file.jpg" in MOVIES.

TO ADD OR CHANGE MOVIES, SHOW TIMES OR FOOD: edit the MOVIES and FOOD lists below, then restart the server.

Order of this file:
    1. Setup, movie list and database tables
    2. Booking rules (book_seats, cancel_booking)
    3. Confirmation email
    4. Accounts (passwords, sessions)
    5. API URLs (signup, login, movies, seats, book, cancel)
    6. Home page (serves index.html)
    7. Built-in tests
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from email.message import EmailMessage
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, func, inspect, Column, Float, Integer, String, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker


# ============================================================ 1. SETUP, MOVIES, DATABASE
if "--test" in sys.argv:   # test mode uses a throwaway database, never your real data
    os.environ["BOOKING_DB"] = "test_booking_app.db"
    os.environ["SMTP_HOST"] = ""
    for _k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
        os.environ[_k] = ""   # tests always start in demo-payment mode
    if os.path.exists("test_booking_app.db"):
        os.remove("test_booking_app.db")

FRONTEND_FILE = Path(__file__).with_name("index.html")   # the web page, next to this file
POSTER_DIR = Path(__file__).with_name("posters")          # optional real poster images (see MOVIES below)

# >>> Edit this list to change the movies shown in the poster slider (sample data). <<<
# name / language / times : shown on the page and used for booking
# price                    : ticket price in rupees, per person (sample prices - set your own)
# genre, emoji, colors     : used to draw the poster when there is no poster image
# release                  : "YYYY-MM-DD" - the poster says "Now showing" or "Releasing <date>"
# poster (optional)        : file name of a real poster image that you put in the "posters" folder
#                            next to this file, e.g. "poster": "ramayana.jpg"
# Titles and release dates are from a November 2026 releases list; languages marked
# "Multi-language" and all show times are placeholders - set them to your cinema's real data.
MOVIES = [
    {"name": "Ramayana: Rise of a Legend", "price": 300, "language": "Multi-language", "genre": "Epic", "emoji": "\U0001F3F9",
     "colors": ["#c2410c", "#7c2d12"], "release": "2026-11-05", "poster": "", "times": ["10:00 AM", "2:00 PM", "6:30 PM", "10:00 PM"]},
    {"name": "The Cat in the Hat", "price": 180, "language": "English", "genre": "Animated", "emoji": "\U0001F431",
     "colors": ["#ef4444", "#1d4ed8"], "release": "2026-11-06", "poster": "", "times": ["10:30 AM", "1:00 PM", "4:00 PM"]},
    {"name": "Mookuthi Amman 2", "price": 200, "language": "Tamil", "genre": "Comedy-drama", "emoji": "\U0001F531",
     "colors": ["#db2777", "#6b21a8"], "release": "2026-11-06", "poster": "", "times": ["11:00 AM", "3:00 PM", "7:00 PM"]},
    {"name": "Ghost Soldier", "price": 220, "language": "Multi-language", "genre": "Military action", "emoji": "\U0001FA96",
     "colors": ["#166534", "#14532d"], "release": "2026-11-12", "poster": "", "times": ["12:00 PM", "4:30 PM", "9:00 PM"]},
    {"name": "I Play Rocky", "price": 220, "language": "Multi-language", "genre": "New release", "emoji": "\U0001F94A",
     "colors": ["#b91c1c", "#1f2937"], "release": "2026-11-13", "poster": "", "times": ["11:30 AM", "3:30 PM", "8:00 PM"]},
    {"name": "How to Rob a Bank", "price": 250, "language": "English", "genre": "Action comedy", "emoji": "\U0001F4B0",
     "colors": ["#0f766e", "#134e4a"], "release": "2026-11-19", "poster": "", "times": ["1:00 PM", "5:00 PM", "9:30 PM"]},
    {"name": "Ebenezer: A Christmas Carol", "price": 220, "language": "English", "genre": "Gothic horror", "emoji": "\U0001F47B",
     "colors": ["#5b21b6", "#0f172a"], "release": "2026-11-20", "poster": "", "times": ["2:30 PM", "6:00 PM", "10:00 PM"]},
    {"name": "The Hunger Games: Sunrise on the Reaping", "price": 300, "language": "English", "genre": "New release", "emoji": "\U0001F525",
     "colors": ["#b45309", "#111827"], "release": "2026-11-20", "poster": "", "times": ["10:00 AM", "1:30 PM", "5:00 PM", "8:30 PM"]},
    {"name": "Godzilla Minus Zero", "price": 280, "language": "Multi-language", "genre": "Kaiju", "emoji": "\U0001F996",
     "colors": ["#1e3a8a", "#0f172a"], "release": "2026-11-20", "poster": "", "times": ["11:00 AM", "3:00 PM", "7:30 PM"]},
    {"name": "Suryaa: Ant Se Aarambh Tak", "price": 220, "language": "Hindi", "genre": "Crime thriller", "emoji": "\U0001F575",
     "colors": ["#7f1d1d", "#1f2937"], "release": "2026-11-20", "poster": "", "times": ["12:30 PM", "4:00 PM", "8:00 PM"]},
    {"name": "Disney's Hexed", "price": 200, "language": "English", "genre": "New release", "emoji": "\U0001FA84",
     "colors": ["#0284c7", "#6d28d9"], "release": "2026-11-27", "poster": "", "times": ["10:30 AM", "1:00 PM", "4:30 PM"]},
]

# >>> Edit this list to change the food menu (sample data, prices in rupees). <<<
# "category" decides which tab of the food picker the item appears under.
FOOD = [
    {"id": "popcorn-regular", "category": "Snacks", "name": "Popcorn (Regular)", "price": 150, "icon": "\U0001F37F"},
    {"id": "popcorn-large", "category": "Snacks", "name": "Popcorn (Large)", "price": 250, "icon": "\U0001F37F"},
    {"id": "popcorn-caramel", "category": "Snacks", "name": "Caramel Popcorn", "price": 220, "icon": "\U0001F36F"},
    {"id": "nachos", "category": "Snacks", "name": "Nachos with Cheese", "price": 180, "icon": "\U0001F9C0"},
    {"id": "french-fries", "category": "Snacks", "name": "French Fries", "price": 140, "icon": "\U0001F35F"},
    {"id": "samosa", "category": "Snacks", "name": "Samosa (2 pcs)", "price": 80, "icon": "\U0001F95F"},
    {"id": "veg-burger", "category": "Meals", "name": "Veg Burger", "price": 160, "icon": "\U0001F354"},
    {"id": "veg-sandwich", "category": "Meals", "name": "Veg Sandwich", "price": 130, "icon": "\U0001F96A"},
    {"id": "pizza-slice", "category": "Meals", "name": "Pizza Slice", "price": 170, "icon": "\U0001F355"},
    {"id": "hot-dog", "category": "Meals", "name": "Hot Dog", "price": 150, "icon": "\U0001F32D"},
    {"id": "cold-drink", "category": "Drinks", "name": "Cold Drink", "price": 100, "icon": "\U0001F964"},
    {"id": "iced-tea", "category": "Drinks", "name": "Iced Tea", "price": 110, "icon": "\U0001F9CB"},
    {"id": "coffee", "category": "Drinks", "name": "Coffee", "price": 90, "icon": "\u2615"},
    {"id": "juice", "category": "Drinks", "name": "Fresh Juice", "price": 120, "icon": "\U0001F9C3"},
    {"id": "water", "category": "Drinks", "name": "Water Bottle", "price": 40, "icon": "\U0001F4A7"},
    {"id": "ice-cream", "category": "Desserts", "name": "Ice Cream Cup", "price": 120, "icon": "\U0001F368"},
    {"id": "brownie", "category": "Desserts", "name": "Chocolate Brownie", "price": 110, "icon": "\U0001F36B"},
    {"id": "donut", "category": "Desserts", "name": "Donut", "price": 90, "icon": "\U0001F369"},
]
FOOD_BY_ID = {f["id"]: f for f in FOOD}
MAX_FOOD_QTY = 10   # most of one food item in a single booking

REVIEW_DELAY_SECONDS = int(os.getenv("REVIEW_DELAY_SECONDS", "120"))   # review email goes out this long after booking
APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8000").rstrip("/")     # used for the link inside the review email
MAX_REVIEW_CHARS = 500

# ---- payments (see the PAYMENTS note at the top of this file)
TICKET_PRICE_DEFAULT = 200                                               # rupees, used if a movie has no "price"
PAYMENT_HOLD_SECONDS = int(os.getenv("PAYMENT_HOLD_SECONDS", "300"))    # how long seats are held while paying
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")                      # public key (safe to send to the browser)
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")              # SECRET: never put it in index.html or in git
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")      # optional, for the webhook
RAZORPAY_API = os.getenv("RAZORPAY_API_BASE", "https://api.razorpay.com/v1").rstrip("/")
WAIT_SECONDS = PAYMENT_HOLD_SECONDS * 3   # a person waits in the line at most this long
PAYMENT_MODE = "razorpay" if (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET) else "demo"

engine = create_engine(
    "sqlite:///" + os.getenv("BOOKING_DB", "booking_app.db"),
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

SEATS = [f"{r}{n}" for r in "ABCDE" for n in range(1, 9)]   # 5 rows x 8 seats
SEATS_PER_ROW = 8
MAX_PARTY = 6   # most people one booking can cover
ITERATIONS = 600_000
USERNAME_RE = re.compile(r"[A-Za-z0-9_]{3,20}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_RE = re.compile(r"[6-9]\d{9}")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)  # stored lowercase
    email = Column(String, unique=True, nullable=False)
    phone = Column(String, unique=True, nullable=False)
    salt = Column(String, nullable=False)
    pw_hash = Column(String, nullable=False)


class Movie(Base):
    __tablename__ = "movies"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    language = Column(String, nullable=False)


class Show(Base):
    """One show = one movie at one time. Every show has its own seat map."""
    __tablename__ = "shows"
    __table_args__ = (UniqueConstraint("movie_id", "time", name="uq_show_movie_time"),)
    id = Column(Integer, primary_key=True)
    movie_id = Column(Integer, nullable=False, index=True)
    time = Column(String, nullable=False)   # e.g. "6:00 PM"


class Ticket(Base):
    """One row per seat. Tickets booked together share the same `ref`."""
    __tablename__ = "tickets"
    __table_args__ = (UniqueConstraint("show_id", "seat", name="uq_ticket_show_seat"),)
    id = Column(Integer, primary_key=True)
    show_id = Column(Integer, nullable=False)
    seat = Column(String, nullable=False)
    user_id = Column(Integer, nullable=False)
    ref = Column(String, nullable=False, index=True)
    attendee = Column(String, nullable=False)


class FoodOrder(Base):
    """Food ordered together with a booking. One row per food item; `ref` links it to the tickets."""
    __tablename__ = "food_orders"
    id = Column(Integer, primary_key=True)
    ref = Column(String, nullable=False, index=True)
    item = Column(String, nullable=False)     # the item name when it was ordered
    qty = Column(Integer, nullable=False)
    price = Column(Integer, nullable=False)   # price of ONE item when it was ordered


class Review(Base):
    """A star rating + comment. One review per person per movie (reviewing again edits it)."""
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("movie_id", "user_id", name="uq_review_movie_user"),)
    id = Column(Integer, primary_key=True)
    movie_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False)
    rating = Column(Integer, nullable=False)          # 1 to 5
    comment = Column(String, nullable=False, default="")
    created_at = Column(Float, nullable=False)        # unix time of the last save


class ReviewInvite(Base):
    """Created with every booking: lets that user review the movie, and says when to send the review email."""
    __tablename__ = "review_invites"
    id = Column(Integer, primary_key=True)
    ref = Column(String, nullable=False, unique=True)   # the booking it belongs to
    user_id = Column(Integer, nullable=False, index=True)
    movie_id = Column(Integer, nullable=False)
    due_at = Column(Float, nullable=False)              # unix time when the review email should be sent
    sent = Column(Integer, nullable=False, default=0)   # 1 once the email has been sent


class PaymentOrder(Base):
    """One per checkout. Status: pending -> paid, or pending -> expired / cancelled (then the seats are freed);
    paid -> refunded. "refund_needed" = money arrived but the seats were gone, and the automatic refund failed."""
    __tablename__ = "payment_orders"
    id = Column(Integer, primary_key=True)
    ref = Column(String, nullable=False, unique=True)          # the booking reference (same as on the tickets)
    user_id = Column(Integer, nullable=False, index=True)
    show_id = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)                   # rupees, worked out by the server
    provider = Column(String, nullable=False)                  # "razorpay" or "demo"
    provider_order_id = Column(String, unique=True)            # Razorpay's order id (set right after checkout)
    payment_id = Column(String)                                # set when paid
    status = Column(String, nullable=False, index=True)
    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)                 # unpaid seats are released after this time
    paid_at = Column(Float)
    payload = Column(String, nullable=False)                   # JSON: what was booked (used if payment arrives late)


class WaitlistEntry(Base):
    """Someone who wants seats that another person is paying for right now.
    Status: waiting -> promoting -> ready (the seats are now held for them; they pay), or failed / cancelled / expired."""
    __tablename__ = "waitlist"
    id = Column(Integer, primary_key=True)
    show_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    payload = Column(String, nullable=False)                   # JSON: first seat, names, food
    status = Column(String, nullable=False, index=True)
    ref = Column(String)                                       # the booking ref, once their turn came
    message = Column(String)                                   # why it failed (shown to the person)
    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)


def upgrade_old_database() -> None:
    """If booking_app.db was made by an older version of this app, replace the outdated
    movie / ticket tables with the current layout. User accounts are kept; old bookings are cleared."""
    insp = inspect(engine)
    tables = insp.get_table_names()
    if "tickets" in tables and "show_id" not in {c["name"] for c in insp.get_columns("tickets")}:
        Ticket.__table__.drop(engine)    # old tickets belonged to a movie, new ones belong to a show
    if "movies" in tables and "time" in {c["name"] for c in insp.get_columns("movies")}:
        Movie.__table__.drop(engine)     # old movies had one fixed time, new ones have many shows


upgrade_old_database()
Base.metadata.create_all(engine)
with SessionLocal() as _db:   # add any movie / show time from MOVIES that is not in the database yet
    for _m in MOVIES:
        _movie = _db.query(Movie).filter_by(name=_m["name"]).first()
        if not _movie:
            _movie = Movie(name=_m["name"], language=_m["language"])
            _db.add(_movie)
            _db.flush()   # gives the new movie its id
        for _t in _m["times"]:
            if not _db.query(Show).filter_by(movie_id=_movie.id, time=_t).first():
                _db.add(Show(movie_id=_movie.id, time=_t))
    _db.commit()


# ============================================================ 2. BOOKING RULES
class SeatTaken(Exception): pass
class NotEnoughSeats(Exception): pass
class NotFound(Exception): pass


def clean_food(food) -> list:
    """Check a food order like [{"id": "nachos", "qty": 2}] and return the lines to save.
    Items with quantity 0 are ignored. Raises ValueError for unknown items or bad quantities."""
    totals: dict = {}
    for item in food or []:
        totals[item["id"]] = totals.get(item["id"], 0) + item["qty"]
    lines = []
    for fid, qty in totals.items():
        if qty == 0:
            continue
        menu_item = FOOD_BY_ID.get(fid)
        if not menu_item:
            raise ValueError("Unknown food item")
        if not 1 <= qty <= MAX_FOOD_QTY:
            raise ValueError(f"You can order 1 to {MAX_FOOD_QTY} of each food item")
        lines.append({"name": menu_item["name"], "qty": qty, "price": menu_item["price"]})
    return lines


class PaymentError(Exception): pass


def ticket_price(show_id: int) -> int:
    """Price of ONE ticket for a show, in rupees: the movie's "price" in the MOVIES list."""
    with SessionLocal() as db:
        show = db.get(Show, show_id)
        movie = db.get(Movie, show.movie_id) if show else None
    meta = next((m for m in MOVIES if movie and m["name"] == movie.name), None)
    return int(meta.get("price", TICKET_PRICE_DEFAULT)) if meta else TICKET_PRICE_DEFAULT


def _insert_rows(db, ref, user_id, show_id, seats, names, food_lines) -> None:
    for seat, name in zip(seats, names):
        db.add(Ticket(show_id=show_id, seat=seat, user_id=user_id, ref=ref, attendee=name))
    for line in food_lines:
        db.add(FoodOrder(ref=ref, item=line["name"], qty=line["qty"], price=line["price"]))


def _review_invite(db, order) -> None:
    """A paid booking lets the user review the movie, and schedules the review email."""
    show = db.get(Show, order.show_id)
    if show and not db.query(ReviewInvite).filter_by(ref=order.ref).first():
        db.add(ReviewInvite(ref=order.ref, user_id=order.user_id, movie_id=show.movie_id,
                            due_at=time.time() + REVIEW_DELAY_SECONDS, sent=0))


def party_seats(start_seat: str, names: list):
    """Check the party and return (clean names, the seats it needs: start_seat and the ones to its right in the row)."""
    names = [n.strip() for n in names]
    if not 1 <= len(names) <= MAX_PARTY:
        raise ValueError(f"You can book 1 to {MAX_PARTY} people at a time")
    if any(not 1 <= len(n) <= 40 for n in names):
        raise ValueError("Enter a name (1 to 40 characters) for every person")
    if start_seat not in SEATS:
        raise ValueError("Unknown seat")
    idx = SEATS.index(start_seat)
    if idx % SEATS_PER_ROW + len(names) > SEATS_PER_ROW:
        raise NotEnoughSeats()
    return names, SEATS[idx: idx + len(names)]


def hold_seats(show_id: int, start_seat: str, user_id: int, names: list, food=None) -> dict:
    """Step 1 of a booking: hold len(names) seats in one row, starting at start_seat, plus an optional food order,
    and work out the price. All or nothing. The seats are NOT confirmed until the payment arrives
    (see confirm_payment); if it never does they are released after PAYMENT_HOLD_SECONDS."""
    names, seats = party_seats(start_seat, names)
    food_lines = clean_food(food)
    ref = "BK-" + secrets.token_hex(3).upper()
    release_expired_holds()   # seats whose payment window ran out are free again

    ticket_total = ticket_price(show_id) * len(names)
    food_total = sum(l["qty"] * l["price"] for l in food_lines)
    now = time.time()
    db = SessionLocal()
    try:
        if not db.get(Show, show_id):
            raise ValueError("Show not found")
        _insert_rows(db, ref, user_id, show_id, seats, names, food_lines)
        db.add(PaymentOrder(ref=ref, user_id=user_id, show_id=show_id, amount=ticket_total + food_total,
                            provider=PAYMENT_MODE, status="pending", created_at=now,
                            expires_at=now + PAYMENT_HOLD_SECONDS,
                            payload=json.dumps({"show_id": show_id, "seats": seats, "names": names, "food": food_lines})))
        db.commit()   # one transaction: the UNIQUE constraint picks the winner
        return {"ref": ref, "seats": seats, "food": food_lines, "food_total": food_total,
                "ticket_total": ticket_total, "amount": ticket_total + food_total}
    except IntegrityError:
        db.rollback()   # nothing from this group is kept
        raise SeatTaken()
    finally:
        db.close()


def confirm_payment(ref: str, payment_id: str) -> str:
    """Step 2: the money has arrived and was verified. Returns "confirmed" (this call did it), "already"
    (done before, e.g. by the webhook), "refund_needed" (paid too late and the seats are gone) or "invalid"."""
    with SessionLocal() as db:
        order = db.query(PaymentOrder).filter_by(ref=ref).first()
        status = order.status if order else None
    if status is None or status in ("refunded", "refund_needed"):
        return "invalid"
    if status in ("paid", "reinstating"):
        return "already"
    if status == "pending":
        with SessionLocal() as db:
            claimed = db.query(PaymentOrder).filter(PaymentOrder.ref == ref, PaymentOrder.status == "pending") \
                .update({"status": "paid", "payment_id": payment_id, "paid_at": time.time()})
            if claimed:
                _review_invite(db, db.query(PaymentOrder).filter_by(ref=ref).first())
            db.commit()
        return "confirmed" if claimed else confirm_payment(ref, payment_id)   # lost a race: look again

    # "expired" or "cancelled": the customer paid after the seats were released. Try to take the same seats again.
    with SessionLocal() as db:
        claimed = db.query(PaymentOrder).filter(PaymentOrder.ref == ref, PaymentOrder.status.in_(["expired", "cancelled"])) \
            .update({"status": "reinstating", "payment_id": payment_id}, synchronize_session=False)
        db.commit()
    if not claimed:
        return confirm_payment(ref, payment_id)
    with SessionLocal() as db:
        order = db.query(PaymentOrder).filter_by(ref=ref).first()
        p = json.loads(order.payload)
        try:
            _insert_rows(db, ref, order.user_id, p["show_id"], p["seats"], p["names"], p["food"])
            order.status, order.paid_at = "paid", time.time()
            _review_invite(db, order)
            db.commit()
            return "confirmed"
        except IntegrityError:
            db.rollback()
    with SessionLocal() as db:
        db.query(PaymentOrder).filter_by(ref=ref).update({"status": "refund_needed"})
        db.commit()
    return "refund_needed"


def release_expired_holds(now=None) -> int:
    """Free the seats of every booking that was not paid within PAYMENT_HOLD_SECONDS. Returns how many were freed."""
    now = time.time() if now is None else now
    with SessionLocal() as db:
        due = [o.ref for o in db.query(PaymentOrder).filter(PaymentOrder.status == "pending", PaymentOrder.expires_at <= now)]
    freed = 0
    for ref in due:
        with SessionLocal() as db:
            claimed = db.query(PaymentOrder).filter(PaymentOrder.ref == ref, PaymentOrder.status == "pending") \
                .update({"status": "expired"})
            if claimed:   # the payment did not arrive in time (if it arrives later, confirm_payment tries to re-book)
                db.query(Ticket).filter(Ticket.ref == ref).delete()
                db.query(FoodOrder).filter(FoodOrder.ref == ref).delete()
                freed += 1
            db.commit()
    return freed


def booking_summary(ref: str):
    """Everything about a PAID booking (for the confirmation email and the reply to the page), or None."""
    with SessionLocal() as db:
        order = db.query(PaymentOrder).filter_by(ref=ref).first()
        if not order or order.status != "paid":
            return None
        user, show = db.get(User, order.user_id), db.get(Show, order.show_id)
        movie = db.get(Movie, show.movie_id) if show else None
        if not (user and show and movie):
            return None
        tickets = db.query(Ticket).filter_by(ref=ref).order_by(Ticket.id).all()
        food = [{"name": f.item, "qty": f.qty, "price": f.price}
                for f in db.query(FoodOrder).filter_by(ref=ref).order_by(FoodOrder.id)]
        return {"ref": ref, "email": user.email, "username": user.username, "movie": movie.name, "time": show.time,
                "seats": [t.seat for t in tickets], "names": [t.attendee for t in tickets], "food": food,
                "amount": order.amount, "payment_id": order.payment_id}


# ---- the waiting line for seats that someone else is paying for right now
def seat_availability(show_id: int, seats: list) -> str:
    """"free", "busy" (every needed seat is only HELD by someone who has not paid yet: worth waiting for)
    or "taken" (at least one seat is paid for: no point waiting)."""
    with SessionLocal() as db:
        refs = [r[0] for r in db.query(Ticket.ref).filter(Ticket.show_id == show_id, Ticket.seat.in_(seats))]
        if not refs:
            return "free"
        pending = {o.ref for o in db.query(PaymentOrder).filter(PaymentOrder.ref.in_(refs), PaymentOrder.status == "pending")}
    return "busy" if all(r in pending for r in refs) else "taken"


def _entry_seats(entry) -> list:
    p = json.loads(entry.payload)
    return party_seats(p["seat"], p["names"])[1]


def _waiters(db, show_id: int):
    """Everyone still in the line for a show, oldest first."""
    return db.query(WaitlistEntry).filter(WaitlistEntry.show_id == show_id,
                                          WaitlistEntry.status.in_(["waiting", "promoting"])) \
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id).all()


def seats_have_waiters(show_id: int, seats: list) -> bool:
    with SessionLocal() as db:
        return any(set(_entry_seats(e)) & set(seats) for e in _waiters(db, show_id))


def begin_hold(show_id: int, uid: int, start_seat: str, names: list, food: list) -> str:
    """Hold the seats AND open the payment order. Returns the booking ref (raises SeatTaken, ValueError, PaymentError)."""
    held = hold_seats(show_id, start_seat, uid, names, food)
    try:
        order_id = create_provider_order(held["ref"], held["amount"])
    except PaymentError:
        cancel_booking(held["ref"], uid)   # let the seats go again
        raise
    with SessionLocal() as db:
        db.query(PaymentOrder).filter_by(ref=held["ref"]).update({"provider_order_id": order_id})
        db.commit()
    return held["ref"]


def join_waitlist(show_id: int, uid: int, start_seat: str, names: list, food=None) -> int:
    """Stand in the line for seats someone else is paying for. Returns the id of the line entry."""
    names, _ = party_seats(start_seat, names)
    clean_food(food)   # reject a bad food order now, not when the turn comes
    now = time.time()
    with SessionLocal() as db:
        if db.query(WaitlistEntry).filter(WaitlistEntry.show_id == show_id, WaitlistEntry.user_id == uid,
                                          WaitlistEntry.status.in_(["waiting", "promoting", "ready"])).first():
            raise ValueError("You are already in the line for this show. Leave it first to choose other seats")
        entry = WaitlistEntry(show_id=show_id, user_id=uid, status="waiting", created_at=now, expires_at=now + WAIT_SECONDS,
                              payload=json.dumps({"seat": start_seat, "names": names, "food": food or []}))
        db.add(entry)
        db.commit()
        return entry.id


def request_seats(show_id: int, uid: int, start_seat: str, names: list, food=None) -> dict:
    """What happens when someone clicks Pay: {"type": "hold", "ref"} when they get the seats at once, or
    {"type": "queued", "id"} when another person is paying for them. Raises SeatTaken if the seats are paid for."""
    names, seats = party_seats(start_seat, names)
    process_waitlist()   # let people who were already waiting go first
    food = food or []
    if not seats_have_waiters(show_id, seats):
        try:
            return {"type": "hold", "ref": begin_hold(show_id, uid, start_seat, names, food)}
        except SeatTaken:
            if seat_availability(show_id, seats) != "busy":
                raise
    return {"type": "queued", "id": join_waitlist(show_id, uid, start_seat, names, food)}


def _try_promote(wid: int) -> None:
    """If the seats of waiting entry wid are free now, hold them for that person (their payment window opens)."""
    with SessionLocal() as db:   # claim it, so two threads cannot promote the same person
        claimed = db.query(WaitlistEntry).filter(WaitlistEntry.id == wid, WaitlistEntry.status == "waiting") \
            .update({"status": "promoting"})
        db.commit()
        entry = db.get(WaitlistEntry, wid) if claimed else None
        if not entry:
            return
        p, show_id, uid = json.loads(entry.payload), entry.show_id, entry.user_id
    status, ref, message = "waiting", None, None
    try:
        ref = begin_hold(show_id, uid, p["seat"], p["names"], p["food"])
        status = "ready"
    except SeatTaken:
        if seat_availability(show_id, _entry_seats_from(p)) == "taken":
            status, message = "failed", "Sorry, the person before you paid, so these seats are booked. Please choose other seats."
        # otherwise somebody else is still paying: keep waiting
    except (ValueError, NotEnoughSeats) as exc:
        status, message = "failed", str(exc) or "These seats cannot be booked"
    except PaymentError:
        status, message = "failed", "The payment service is not available right now. Please try again."
    with SessionLocal() as db:
        db.query(WaitlistEntry).filter_by(id=wid).update({"status": status, "ref": ref, "message": message})
        db.commit()


def _entry_seats_from(payload: dict) -> list:
    return party_seats(payload["seat"], payload["names"])[1]


def process_waitlist(now=None) -> None:
    """Free unpaid holds, drop people who waited too long, then give the seats to the people in the line, oldest first."""
    now = time.time() if now is None else now
    release_expired_holds(now)
    with SessionLocal() as db:
        db.query(WaitlistEntry).filter(WaitlistEntry.status == "waiting", WaitlistEntry.expires_at <= now) \
            .update({"status": "expired", "message": "You waited too long and the seats did not become free."})
        db.commit()
        ids = [e.id for e in db.query(WaitlistEntry).filter(WaitlistEntry.status == "waiting")
               .order_by(WaitlistEntry.created_at, WaitlistEntry.id)]
    for wid in ids:
        _try_promote(wid)


def leave_waitlist(wid: int, uid: int) -> None:
    """Leave the line. If it was already your turn, the held seats are released too."""
    with SessionLocal() as db:
        entry = db.query(WaitlistEntry).filter_by(id=wid, user_id=uid).first()
        if not entry:
            raise NotFound()
        status, ref = entry.status, entry.ref
        if status in ("waiting", "promoting", "ready"):
            entry.status = "cancelled"
            db.commit()
    if status == "ready" and ref:
        try:
            cancel_booking(ref, uid)   # only if still unpaid: a paid booking must be cancelled (and refunded) on purpose
        except (NotFound, PaymentError):
            pass
    process_waitlist()


# ---- talking to the payment provider (Razorpay). Everything that leaves this server goes through razorpay_request.
def razorpay_request(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    token = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req = urllib.request.Request(RAZORPAY_API + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + token})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise PaymentError(f"Razorpay said {exc.code}: {exc.read().decode(errors='replace')[:200]}")
    except Exception as exc:
        raise PaymentError(f"Could not reach Razorpay ({type(exc).__name__})")


def create_provider_order(ref: str, amount: int) -> str:
    """Create the payment order at the provider and return its id."""
    if PAYMENT_MODE == "razorpay":
        res = razorpay_request("/orders", {"amount": amount * 100, "currency": "INR", "receipt": ref})   # amount is in paise
        if not res.get("id"):
            raise PaymentError("Razorpay did not return an order id")
        return res["id"]
    return "demo_order_" + secrets.token_hex(6)


def verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Razorpay signs 'order_id|payment_id' with your secret. Only a real, completed payment has a matching signature."""
    if not (RAZORPAY_KEY_SECRET and order_id and payment_id and signature):
        return False
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def valid_webhook_signature(body: bytes, signature: str) -> bool:
    if not (RAZORPAY_WEBHOOK_SECRET and signature):
        return False   # the webhook stays switched off until a webhook secret is configured
    return hmac.compare_digest(hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest(), signature)


def refund_order(ref: str) -> int:
    """Refund a paid order in full. Raises PaymentError (and changes nothing) if the provider refuses."""
    with SessionLocal() as db:
        order = db.query(PaymentOrder).filter_by(ref=ref).first()
        if not order or order.status not in ("paid", "refund_needed"):
            raise PaymentError("There is nothing to refund for this booking")
        provider, payment_id, amount = order.provider, order.payment_id, order.amount
    if provider == "razorpay":
        if not payment_id:
            raise PaymentError("The payment id is missing")
        razorpay_request(f"/payments/{payment_id}/refund", {"amount": amount * 100, "notes": {"booking": ref}})
    with SessionLocal() as db:
        db.query(PaymentOrder).filter_by(ref=ref).update({"status": "refunded"})
        db.commit()
    return amount


def settle_payment(ref: str, payment_id: str) -> dict:
    """A verified payment came in. Confirm the booking; if it is too late, refund it automatically."""
    status = confirm_payment(ref, payment_id)
    if status == "refund_needed":
        try:
            refund_order(ref)
            status = "refunded"
        except PaymentError as exc:   # money is with us but could not be returned: needs a human
            print(f"!!! REFUND NEEDED for booking {ref}, payment {payment_id}: {exc}")
    summary = booking_summary(ref) if status in ("confirmed", "already") else None
    return {"status": status, "summary": summary}


def cancel_booking(ref: str, user_id: int) -> dict:
    """Cancel a booking and free its seats. An unpaid hold is simply released. A paid booking is refunded in
    full first; if the refund fails nothing is cancelled. Returns {"refunded": rupees}."""
    with SessionLocal() as db:
        if not db.query(Ticket).filter(Ticket.ref == ref, Ticket.user_id == user_id).first():
            raise NotFound()
        order = db.query(PaymentOrder).filter_by(ref=ref).first()
        status, amount = (order.status, order.amount) if order else (None, 0)
    refunded = 0
    if status == "pending":   # not paid yet (unless the payment lands this very moment)
        with SessionLocal() as db:
            claimed = db.query(PaymentOrder).filter(PaymentOrder.ref == ref, PaymentOrder.status == "pending") \
                .update({"status": "cancelled"})
            db.commit()
        status = "cancelled" if claimed else "paid"
    if status == "paid":
        refunded = refund_order(ref)
    with SessionLocal() as db:
        db.query(Ticket).filter(Ticket.ref == ref, Ticket.user_id == user_id).delete()
        db.query(FoodOrder).filter(FoodOrder.ref == ref).delete()   # the food goes with the tickets
        db.query(ReviewInvite).filter(ReviewInvite.ref == ref).delete()   # no review mail for a cancelled booking
        db.commit()
    return {"refunded": refunded}


class ReviewNotAllowed(Exception): pass


def can_review(user_id: int, movie_id: int) -> bool:
    """Only people who booked the movie (and did not cancel every booking) may review it."""
    with SessionLocal() as db:
        return db.query(ReviewInvite).filter_by(user_id=user_id, movie_id=movie_id).first() is not None


def save_review(user_id: int, movie_id: int, rating, comment: str) -> None:
    """Add the user's review of a movie, or replace their earlier one."""
    comment = (comment or "").strip()
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError("Choose a rating from 1 to 5 stars")
    if len(comment) > MAX_REVIEW_CHARS:
        raise ValueError(f"Keep the review under {MAX_REVIEW_CHARS} characters")
    if not can_review(user_id, movie_id):
        raise ReviewNotAllowed()
    with SessionLocal() as db:
        existing = db.query(Review).filter_by(user_id=user_id, movie_id=movie_id).first()
        if existing:
            existing.rating, existing.comment, existing.created_at = rating, comment, time.time()
        else:
            db.add(Review(user_id=user_id, movie_id=movie_id, rating=rating, comment=comment, created_at=time.time()))
        db.commit()


def get_reviews(movie_id: int, user_id: int) -> dict:
    """Everything the page needs to show a movie's reviews (newest first)."""
    with SessionLocal() as db:
        rows = (db.query(Review, User.username).join(User, User.id == Review.user_id)
                .filter(Review.movie_id == movie_id).order_by(Review.created_at.desc()).all())
    reviews = [{"username": name, "rating": r.rating, "comment": r.comment,
                "created_at": r.created_at, "mine": r.user_id == user_id} for r, name in rows]
    count = len(reviews)
    return {"count": count, "average": round(sum(r["rating"] for r in reviews) / count, 1) if count else 0,
            "reviews": reviews, "mine": next((r for r in reviews if r["mine"]), None),
            "can_review": can_review(user_id, movie_id)}


def rating_summary() -> dict:
    """{movie_id: (average rating, number of reviews)} for the stars shown on the posters."""
    with SessionLocal() as db:
        rows = db.query(Review.movie_id, func.avg(Review.rating), func.count(Review.id)).group_by(Review.movie_id).all()
    return {mid: (round(float(avg), 1), cnt) for mid, avg, cnt in rows}


# ============================================================ 3. CONFIRMATION EMAIL
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", "") or SMTP_USER
EMAIL_READY = bool(SMTP_HOST)


def deliver_email(to: str, subject: str, text: str, page: str) -> None:
    """Send one email (text + HTML) with smtplib, or print it in the terminal when SMTP is not set up."""
    if not EMAIL_READY:   # development fallback: show the email in the terminal
        print("\n--- DEV ONLY: email is not set up, so it is printed here ---\n"
              f"To: {to}\n{text}\n--- end of email ---\n")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    try:
        smtp_class = smtplib.SMTP_SSL if SMTP_PORT == 465 else smtplib.SMTP
        with smtp_class(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            if SMTP_PORT != 465:
                server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print("Email sent to", to)
    except Exception as exc:
        print("Email error:", type(exc).__name__, exc)


def send_booking_email(to: str, username: str, movie_name: str, show_time: str,
                       ref: str, seats: list, names: list, food: list = None, payment: dict = None) -> None:
    """Email the booking details. Runs in the background; a failure never undoes the booking."""
    subject = f"Your movie tickets: {movie_name} ({ref})"
    food = food or []
    food_total = sum(f["qty"] * f["price"] for f in food)
    food_text, food_html = "", ""
    if food:   # the food part of the email (empty when no food was ordered)
        food_text = ("\n\nFood order:\n"
                     + "\n".join(f"{f['qty']} x {f['name']}  -  Rs. {f['qty'] * f['price']}" for f in food)
                     + f"\nFood total: Rs. {food_total}")
        frows = "".join(
            f"<tr><td style='padding:6px 14px'>{html.escape(f['name'])}</td>"
            f"<td style='padding:6px 14px'>{f['qty']}</td>"
            f"<td style='padding:6px 14px'>&#8377;{f['qty'] * f['price']}</td></tr>"
            for f in food)
        food_html = ("<p><b>Food order</b></p><table style='border-collapse:collapse' border='1' cellspacing='0'>"
                     "<tr style='background:#eceefc'><th style='padding:6px 14px'>Item</th>"
                     "<th style='padding:6px 14px'>Qty</th><th style='padding:6px 14px'>Amount</th></tr>"
                     f"{frows}</table><p>Food total: <b>&#8377;{food_total}</b></p>")
    pay_text, pay_html = "", ""
    if payment:   # the payment part of the email (empty for bookings made before online payment existed)
        pay_text = f"\n\nPayment received: Rs. {payment['amount']} (payment id {payment['payment_id']})"
        pay_html = (f"<p>Payment received: <b>&#8377;{payment['amount']}</b> "
                    f"<span style='color:#6b7090'>(payment id {html.escape(str(payment['payment_id']))})</span></p>")
    text = (
        f"Hi {username},\n\nYour booking is confirmed.\n\n"
        f"Movie: {movie_name}\nShow time: {show_time}\nBooking reference: {ref}\nTickets: {len(seats)}\n\n"
        "Seat  -  Name\n" + "\n".join(f"{s}  -  {n}" for s, n in zip(seats, names))
        + food_text + pay_text + "\n\nEnjoy the movie!\nSeatBook"
    )
    rows = "".join(
        f"<tr><td style='padding:6px 14px'>{html.escape(s)}</td><td style='padding:6px 14px'>{html.escape(n)}</td></tr>"
        for s, n in zip(seats, names)
    )
    page = (
        "<div style='font-family:Arial,sans-serif;color:#1d2033'>"
        f"<h2>Booking confirmed &#127916;</h2><p>Hi {html.escape(username)}, your seats are booked.</p>"
        f"<p><b>{html.escape(movie_name)}</b> &middot; {html.escape(show_time)}<br>"
        f"Reference: <b>{html.escape(ref)}</b> &middot; {len(seats)} ticket(s)</p>"
        "<table style='border-collapse:collapse' border='1' cellspacing='0'>"
        f"<tr style='background:#eceefc'><th style='padding:6px 14px'>Seat</th><th style='padding:6px 14px'>Name</th></tr>{rows}</table>"
        f"{food_html}{pay_html}<p>Enjoy the movie!<br>SeatBook</p></div>"
    )

    deliver_email(to, subject, text, page)


def send_review_email(to: str, username: str, movie_name: str, movie_id: int) -> None:
    """The second email: asks the user to rate and review the movie. The link opens the site with the review box ready."""
    link = f"{APP_URL}/?review={movie_id}"
    subject = f"How was {movie_name}? Share your review"
    text = (f"Hi {username},\n\nThanks for booking {movie_name} with SeatBook. How was the movie?\n"
            f"Rate it and write a short review (it takes a minute):\n\n{link}\n\n"
            "Your review will be shown to other movie-goers on the movie's page.\nSeatBook")
    page = ("<div style='font-family:Arial,sans-serif;color:#1d2033'>"
            f"<h2>How was {html.escape(movie_name)}? &#11088;</h2>"
            f"<p>Hi {html.escape(username)}, thanks for booking with SeatBook. Rate the movie and write a short review.</p>"
            f"<p><a href='{html.escape(link)}' style='display:inline-block;padding:12px 22px;background:#5b5bf0;color:#fff;"
            "border-radius:8px;text-decoration:none;font-weight:bold'>Write my review</a></p>"
            "<p style='color:#6b7090'>Your review will be shown to other movie-goers on the movie's page.</p>"
            "<p>SeatBook</p></div>")
    deliver_email(to, subject, text, page)


def send_due_review_emails(now=None) -> int:
    """Send every review email whose time has come (each one only once). Returns how many were sent."""
    now = time.time() if now is None else now
    with SessionLocal() as db:
        due_ids = [i.id for i in db.query(ReviewInvite).filter(ReviewInvite.sent == 0, ReviewInvite.due_at <= now)]
    sent = 0
    for invite_id in due_ids:
        with SessionLocal() as db:
            claimed = db.query(ReviewInvite).filter(ReviewInvite.id == invite_id, ReviewInvite.sent == 0).update({"sent": 1})
            db.commit()   # claimed first, so the same mail can never go out twice
            if not claimed:
                continue
            invite = db.get(ReviewInvite, invite_id)
            user, movie = db.get(User, invite.user_id), db.get(Movie, invite.movie_id)
            if not user or not movie:
                continue
            to, username, movie_name, movie_id = user.email, user.username, movie.name, movie.id
        send_review_email(to, username, movie_name, movie_id)
        sent += 1
    return sent


def background_worker() -> None:
    """Background loop, every 2 seconds: free the seats of unpaid bookings and send the review emails that are due."""
    while True:
        try:
            process_waitlist()   # frees unpaid holds and gives seats to people waiting for them
            send_due_review_emails()
        except Exception as exc:   # never let the loop die
            print("Background worker error:", type(exc).__name__, exc)
        time.sleep(2)


# ============================================================ 4. ACCOUNTS
SESSIONS: dict = {}   # token -> {uid, expires}   (lost on server restart)
FAILED: dict = {}     # username -> {n, until}    (simple brute-force lockout)


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), ITERATIONS
    ).hex()


def new_session(uid: int) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"uid": uid, "expires": time.time() + 8 * 3600}
    return token


def current_user(authorization: str = Header(default="")) -> int:
    s = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not s or s["expires"] < time.time():
        raise HTTPException(401, "Please log in again")
    return s["uid"]


class SignupIn(BaseModel):
    username: str
    email: str
    phone: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class FoodIn(BaseModel):
    id: str
    qty: int


class BookIn(BaseModel):
    seat: str
    names: list[str]
    food: list[FoodIn] = []   # optional food order


# ============================================================ 5. API URLS
@asynccontextmanager
async def lifespan(_app):
    if "--test" not in sys.argv:   # the built-in tests call these functions themselves
        threading.Thread(target=background_worker, daemon=True, name="background-worker").start()
    yield


app = FastAPI(title="SeatBook", lifespan=lifespan)
POSTER_DIR.mkdir(exist_ok=True)
app.mount("/posters", StaticFiles(directory=POSTER_DIR), name="posters")   # serves the poster images


@app.post("/api/signup")
def signup(data: SignupIn):
    username, email, phone = data.username.strip().lower(), data.email.strip().lower(), data.phone.strip()
    pw = data.password
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(400, "Username must be 3 to 20 letters, numbers or underscores")
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "Enter a valid email address")
    if not PHONE_RE.fullmatch(phone):
        raise HTTPException(400, "Enter a valid 10-digit mobile number")
    if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise HTTPException(400, "Password needs at least 8 characters, with a letter and a number")
    with SessionLocal() as db:
        for col, val, label in ((User.username, username, "Username"),
                                (User.email, email, "Email"),
                                (User.phone, phone, "Phone number")):
            if db.query(User).filter(col == val).first():
                raise HTTPException(409, f"{label} is already registered")
        salt = secrets.token_hex(16)
        user = User(username=username, email=email, phone=phone,
                    salt=salt, pw_hash=hash_password(pw, salt))
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "That account already exists")
        return {"token": new_session(user.id)}


@app.post("/api/login")
def login(data: LoginIn):
    username = data.username.strip().lower()
    lock = FAILED.get(username)
    if lock and lock["until"] > time.time():
        raise HTTPException(429, "Too many failed attempts. Try again in a minute")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).first()
        uid, salt, stored = (user.id, user.salt, user.pw_hash) if user else (None, "00" * 16, "")
    ok = hmac.compare_digest(stored, hash_password(data.password, salt)) and uid is not None
    if not ok:   # same message and same work whether or not the username exists
        f = FAILED.setdefault(username, {"n": 0, "until": 0})
        f["n"] += 1
        if f["n"] >= 5:
            f["n"], f["until"] = 0, time.time() + 60
        raise HTTPException(401, "Invalid username or password")
    FAILED.pop(username, None)
    return {"token": new_session(uid)}


@app.get("/api/me")
def me(uid: int = Depends(current_user)):
    with SessionLocal() as db:
        u = db.get(User, uid)
        if not u:
            raise HTTPException(401, "Please log in again")
        return {"username": u.username, "email": u.email}


@app.get("/api/movies")
def movies(uid: int = Depends(current_user)):
    """The poster slider data, in the order of the MOVIES list at the top of this file."""
    out = []
    ratings = rating_summary()
    with SessionLocal() as db:
        for m in MOVIES:
            row = db.query(Movie).filter_by(name=m["name"]).first()
            if not row:
                continue
            shows = {s.time: s.id for s in db.query(Show).filter_by(movie_id=row.id)}
            has_image = bool(m.get("poster")) and (POSTER_DIR / m["poster"]).is_file()
            out.append({"id": row.id, "name": m["name"], "language": m["language"], "genre": m["genre"],
                        "emoji": m["emoji"], "colors": m["colors"], "release": m["release"],
                        "price": int(m.get("price", TICKET_PRICE_DEFAULT)),
                        "poster": "/posters/" + m["poster"] if has_image else "",
                        "rating_avg": ratings.get(row.id, (0, 0))[0], "rating_count": ratings.get(row.id, (0, 0))[1],
                        "shows": [{"id": shows[t], "time": t} for t in m["times"] if t in shows]})
    return out


class ReviewIn(BaseModel):
    rating: int
    comment: str = ""


@app.get("/api/movies/{mid}/reviews")
def movie_reviews(mid: int, uid: int = Depends(current_user)):
    with SessionLocal() as db:
        if not db.get(Movie, mid):
            raise HTTPException(404, "Movie not found")
    return get_reviews(mid, uid)


@app.post("/api/movies/{mid}/reviews")
def post_review(mid: int, data: ReviewIn, uid: int = Depends(current_user)):
    with SessionLocal() as db:
        if not db.get(Movie, mid):
            raise HTTPException(404, "Movie not found")
    try:
        save_review(uid, mid, data.rating, data.comment)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except ReviewNotAllowed:
        raise HTTPException(403, "You can review a movie after you have booked tickets for it")
    return get_reviews(mid, uid)


@app.get("/api/food")
def food_menu(uid: int = Depends(current_user)):
    """The food menu: [{id, category, name, price, icon}]."""
    return FOOD


@app.get("/api/shows/{sid}/seats")
def seats(sid: int, uid: int = Depends(current_user)):
    """state: free / taken (someone else's, paid) / busy (someone else is paying for it right now: you can join the
    line) / mine (paid) / held (mine, waiting for payment)."""
    process_waitlist()
    with SessionLocal() as db:
        if not db.get(Show, sid):
            raise HTTPException(404, "Show not found")
        rows = [(t.seat, t.user_id, t.ref) for t in db.query(Ticket).filter_by(show_id=sid)]
        pending = {o.ref for o in db.query(PaymentOrder).filter(PaymentOrder.status == "pending",
                                                                PaymentOrder.show_id == sid)}
    state = {seat: ("busy" if ref in pending else "taken") if owner != uid else ("held" if ref in pending else "mine")
             for seat, owner, ref in rows}
    return [{"id": s, "state": state.get(s, "free")} for s in SEATS]


def checkout_response(ref: str) -> dict:
    """Everything the page needs to open the payment window for a held booking."""
    with SessionLocal() as db:
        order = db.query(PaymentOrder).filter_by(ref=ref).first()
        user, show = db.get(User, order.user_id), db.get(Show, order.show_id)
        movie = db.get(Movie, show.movie_id)
        info = {"order": (order.amount, order.provider, order.provider_order_id, order.expires_at, json.loads(order.payload)),
                "movie": movie.name, "time": show.time, "show_id": show.id,
                "prefill": {"name": user.username, "email": user.email, "contact": user.phone}}
    amount, provider, order_id, expires_at, p = info["order"]
    food_total = sum(f["qty"] * f["price"] for f in p["food"])
    return {"queued": False, "ref": ref, "mode": provider, "order_id": order_id, "currency": "INR", "amount": amount,
            "key_id": RAZORPAY_KEY_ID if provider == "razorpay" else "", "expires_in": max(1, int(expires_at - time.time())),
            "movie": info["movie"], "time": info["time"], "seats": p["seats"], "ticket_price": ticket_price(info["show_id"]),
            "ticket_total": amount - food_total, "food": p["food"], "food_total": food_total, "prefill": info["prefill"]}


@app.post("/api/shows/{sid}/checkout")
def checkout(sid: int, data: BookIn, uid: int = Depends(current_user)):
    """Hold the seats and open a payment order (the booking is NOT confirmed until the payment is verified).
    If another person is paying for those seats right now, you join the line instead: {"queued": true, ...}."""
    with SessionLocal() as db:
        if not db.get(Show, sid):
            raise HTTPException(404, "Show not found")
        if not db.get(User, uid):
            raise HTTPException(401, "Please log in again")
    try:
        result = request_seats(sid, uid, data.seat.strip().upper(), data.names, [{"id": f.id, "qty": f.qty} for f in data.food])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except NotEnoughSeats:
        raise HTTPException(400, "Not enough seats to the right in that row. Pick a seat further left")
    except SeatTaken:
        raise HTTPException(409, "Sorry, one of those seats was just booked by someone else. Nothing was booked")
    except PaymentError as exc:
        raise HTTPException(502, f"The payment service is not available right now, please try again. ({str(exc)[:120]})")
    if result["type"] == "hold":
        return checkout_response(result["ref"])
    return waitlist_status(result["id"], uid)


def waitlist_status(wid: int, uid: int) -> dict:
    with SessionLocal() as db:
        entry = db.query(WaitlistEntry).filter_by(id=wid, user_id=uid).first()
        if not entry:
            raise HTTPException(404, "Not found")
        status, ref, message, created, show_id = entry.status, entry.ref, entry.message, entry.created_at, entry.show_id
        seats = _entry_seats(entry)
        ahead = sum(1 for e in _waiters(db, show_id) if e.id != wid and (e.created_at, e.id) < (created, wid)
                    and set(_entry_seats(e)) & set(seats))
        refs = [r[0] for r in db.query(Ticket.ref).filter(Ticket.show_id == show_id, Ticket.seat.in_(seats))]
        holders = [o.expires_at for o in db.query(PaymentOrder).filter(PaymentOrder.ref.in_(refs), PaymentOrder.status == "pending")] if refs else []
    if status == "ready":
        with SessionLocal() as db:
            order = db.query(PaymentOrder).filter_by(ref=ref).first()
            still_pending = bool(order and order.status == "pending")
        if not still_pending:
            return {"queued": True, "id": wid, "status": "expired", "seats": seats,
                    "message": "Your payment time ran out and the seats were released."}
        return {"queued": True, "id": wid, "status": "ready", "seats": seats, "checkout": checkout_response(ref)}
    return {"queued": True, "id": wid, "status": status, "seats": seats, "message": message or "",
            "position": ahead + 1, "wait_up_to": max([0] + [int(t - time.time()) for t in holders])}


@app.get("/api/waitlist/{wid}")
def waitlist_get(wid: int, uid: int = Depends(current_user)):
    """The page asks this every couple of seconds while the person waits for their turn."""
    process_waitlist()
    return waitlist_status(wid, uid)


@app.delete("/api/waitlist/{wid}")
def waitlist_leave(wid: int, uid: int = Depends(current_user)):
    try:
        leave_waitlist(wid, uid)
    except NotFound:
        raise HTTPException(404, "Not found")
    return {"message": "You left the line"}


def booking_response(s: dict) -> dict:
    """What the page needs after a booking is paid and confirmed."""
    masked = s["email"][0] + "***" + s["email"][s["email"].index("@"):]
    note = (f"A confirmation email is on its way to {masked}." if EMAIL_READY
            else "Email is not set up yet, so it was printed in the server terminal.")
    wait = f"{REVIEW_DELAY_SECONDS // 60} min" if REVIEW_DELAY_SECONDS >= 60 else f"{REVIEW_DELAY_SECONDS} sec"
    food_total = sum(f["qty"] * f["price"] for f in s["food"])
    food_note = f" Food ordered: Rs. {food_total}." if s["food"] else ""
    return {"ref": s["ref"], "seats": s["seats"], "food": s["food"], "food_total": food_total,
            "amount": s["amount"], "payment_id": s["payment_id"],
            "message": f"Payment of Rs. {s['amount']} received. Booked {len(s['seats'])} ticket(s) for {s['movie']} "
                       f"at {s['time']}: {', '.join(s['seats'])}.{food_note} {note} A review request will follow in about {wait}."}


def finish_payment(ref: str, payment_id: str, background: BackgroundTasks) -> dict:
    result = settle_payment(ref, payment_id)
    status, summary = result["status"], result["summary"]
    if status == "refunded":
        raise HTTPException(409, "Your payment arrived after the seats were released and they have since been taken, "
                                 "so the full amount has been refunded.")
    if status == "refund_needed":
        raise HTTPException(409, f"Your payment arrived but the seats are no longer available. Please contact support "
                                 f"with booking reference {ref} for a refund.")
    if not summary:
        raise HTTPException(400, "This payment could not be matched to a booking")
    if status == "confirmed":   # first time only, so the emails go out once even if the webhook also fires
        background.add_task(send_booking_email, summary["email"], summary["username"], summary["movie"], summary["time"],
                            ref, summary["seats"], summary["names"], summary["food"],
                            {"amount": summary["amount"], "payment_id": summary["payment_id"]})
    return booking_response(summary)


class VerifyIn(BaseModel):
    order_id: str
    payment_id: str
    signature: str


@app.post("/api/payments/verify")
def verify_payment(data: VerifyIn, background: BackgroundTasks, uid: int = Depends(current_user)):
    """Razorpay mode: the page sends the three values Razorpay gave it after payment. Only a payment whose
    signature checks out (it can be made only with the secret key) confirms the booking."""
    with SessionLocal() as db:
        order = db.query(PaymentOrder).filter_by(provider_order_id=data.order_id).first()
        ref, owner = (order.ref, order.user_id) if order else (None, None)
    if not ref or owner != uid:
        raise HTTPException(404, "Order not found")
    if PAYMENT_MODE != "razorpay" or not verify_signature(data.order_id, data.payment_id, data.signature):
        raise HTTPException(400, "The payment could not be verified")
    return finish_payment(ref, data.payment_id, background)


class DemoPayIn(BaseModel):
    ref: str
    outcome: str = "success"   # "success" or "fail"


@app.post("/api/payments/demo")
def demo_payment(data: DemoPayIn, background: BackgroundTasks, uid: int = Depends(current_user)):
    """DEMO mode only: the practice payment window. Switched off completely once Razorpay keys are set."""
    if PAYMENT_MODE != "demo":
        raise HTTPException(404, "Not found")
    with SessionLocal() as db:
        order = db.query(PaymentOrder).filter_by(ref=data.ref).first()
        owner = order.user_id if order else None
    if owner != uid:
        raise HTTPException(404, "Order not found")
    if data.outcome == "fail":
        raise HTTPException(402, "Payment failed (this is a practice failure). Your seats are still held, so you can try again.")
    return finish_payment(data.ref, "demo_pay_" + secrets.token_hex(5), background)


def process_webhook(body: bytes, signature: str, background: BackgroundTasks) -> bool:
    """Razorpay calls this by itself when a payment succeeds, even if the customer closed the browser."""
    if not valid_webhook_signature(body, signature):
        return False
    event = json.loads(body)
    if event.get("event") in ("payment.captured", "order.paid"):
        payment = event.get("payload", {}).get("payment", {}).get("entity", {})
        order_id, payment_id = payment.get("order_id"), payment.get("id")
        if order_id and payment_id:
            with SessionLocal() as db:
                order = db.query(PaymentOrder).filter_by(provider_order_id=order_id).first()
                ref = order.ref if order else None
            if ref:
                result = settle_payment(ref, payment_id)
                if result["status"] == "confirmed" and result["summary"]:
                    s = result["summary"]
                    background.add_task(send_booking_email, s["email"], s["username"], s["movie"], s["time"], ref,
                                        s["seats"], s["names"], s["food"], {"amount": s["amount"], "payment_id": s["payment_id"]})
    return True


@app.post("/api/razorpay/webhook")
async def razorpay_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    ok = await run_in_threadpool(process_webhook, body, request.headers.get("x-razorpay-signature", ""), background)
    if not ok:
        raise HTTPException(400, "Bad signature")
    return {"ok": True}


@app.get("/api/my/bookings")
def my_bookings(uid: int = Depends(current_user)):
    """Confirmed bookings only. Seats still waiting for payment are not listed here."""
    release_expired_holds()
    with SessionLocal() as db:
        rows = (db.query(Ticket, Show, Movie)
                .join(Show, Show.id == Ticket.show_id)
                .join(Movie, Movie.id == Show.movie_id)
                .filter(Ticket.user_id == uid).order_by(Ticket.id).all())
        orders = {o.ref: o for o in db.query(PaymentOrder).filter(PaymentOrder.user_id == uid)}
    groups: dict = {}
    count = 0
    for t, s, m in rows:
        order = orders.get(t.ref)
        if order and order.status != "paid":
            continue   # not paid (yet)
        g = groups.setdefault(t.ref, {"ref": t.ref, "movie": m.name, "time": s.time, "seats": [], "names": [],
                                      "food": [], "food_total": 0,
                                      "amount": order.amount if order else None,
                                      "payment_id": order.payment_id if order else None})
        g["seats"].append(t.seat)
        g["names"].append(t.attendee)
        count += 1
    if groups:   # add each booking's food order
        with SessionLocal() as db:
            for f in db.query(FoodOrder).filter(FoodOrder.ref.in_(list(groups))).order_by(FoodOrder.id):
                groups[f.ref]["food"].append({"name": f.item, "qty": f.qty, "price": f.price})
                groups[f.ref]["food_total"] += f.qty * f.price
    return {"total_tickets": count, "bookings": list(groups.values())}


@app.delete("/api/bookings/{ref}")
def cancel(ref: str, uid: int = Depends(current_user)):
    try:
        result = cancel_booking(ref, uid)
    except NotFound:
        raise HTTPException(404, "Booking not found")
    except PaymentError as exc:
        raise HTTPException(502, f"We could not process the refund right now, so the booking was NOT cancelled. "
                                 f"Please try again in a moment. ({str(exc)[:120]})")
    process_waitlist()   # the next person in the line (if any) gets their payment window now
    if result["refunded"]:
        return {"message": f"Booking cancelled. Rs. {result['refunded']} is being refunded to your original payment method.",
                "refunded": result["refunded"]}
    return {"message": "Booking cancelled. The seats are free again", "refunded": 0}


# ============================================================ 6. HOME PAGE
@app.get("/")
def home():
    """Send the frontend (index.html) to the browser."""
    return FileResponse(FRONTEND_FILE, media_type="text/html",
                        headers={"Cache-Control": "no-store"})   # always load the newest page


# ============================================================ 7. BUILT-IN TESTS
import unittest


def book_seats(show_id, start_seat, user_id, names, food=None) -> dict:
    """TEST HELPER ONLY (nothing in the web API calls it): hold the seats and mark them paid at once."""
    result = hold_seats(show_id, start_seat, user_id, names, food)
    confirm_payment(result["ref"], "test-payment-" + result["ref"])
    return result


class BookingTests(unittest.TestCase):
    # Show ids 1 and 2 are the first two show times of the first movie (see MOVIES above).

    def test_20_simultaneous_bookings_exactly_one_wins(self):
        n = 20
        barrier = threading.Barrier(n)   # release all threads at the same moment
        results = []

        def worker(i):
            barrier.wait()
            try:
                book_seats(1, "A1", i + 100, [f"Guest {i}"])
                results.append("won")
            except SeatTaken:
                results.append("taken")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), n)
        self.assertEqual(results.count("won"), 1)
        self.assertEqual(results.count("taken"), n - 1)

    def test_group_books_consecutive_seats_in_one_row(self):
        result = book_seats(1, "B1", 1, ["Asha", "Ravi", "Meera"])
        self.assertEqual(result["seats"], ["B1", "B2", "B3"])
        self.assertTrue(result["ref"].startswith("BK-"))

    def test_same_seat_in_different_shows_is_allowed(self):
        book_seats(1, "B5", 1, ["Asha"])
        book_seats(2, "B5", 2, ["Ravi"])   # a different show time, so no conflict

    def test_group_booking_is_all_or_nothing(self):
        book_seats(1, "C3", 1, ["Asha"])
        with self.assertRaises(SeatTaken):
            book_seats(1, "C2", 2, ["Ravi", "Meera", "Kabir"])   # needs C2, C3, C4 but C3 is taken
        book_seats(1, "C2", 3, ["Isha"])    # nothing from the failed group was kept
        book_seats(1, "C4", 3, ["Arjun"])

    def test_group_cannot_run_past_the_end_of_a_row(self):
        with self.assertRaises(NotEnoughSeats):
            book_seats(1, "D8", 1, ["Asha", "Ravi"])

    def test_party_size_and_names_are_validated(self):
        with self.assertRaises(ValueError):
            book_seats(1, "E1", 1, [])
        with self.assertRaises(ValueError):
            book_seats(1, "E1", 1, ["A", "B", "C", "D", "E", "F", "G"])   # more than 6
        with self.assertRaises(ValueError):
            book_seats(1, "E1", 1, ["   "])                               # blank name

    def test_only_the_owner_can_cancel_and_the_seats_are_freed(self):
        ref = book_seats(1, "E3", 1, ["Asha", "Ravi"])["ref"]
        with self.assertRaises(NotFound):
            cancel_booking(ref, 2)           # someone else's booking
        cancel_booking(ref, 1)
        book_seats(1, "E3", 2, ["Meera"])    # seat is free again

    def test_confirmation_email_lists_movie_seats_and_names(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):   # test mode has no SMTP server, so it prints
            send_booking_email("asha@example.com", "asha", "Jawan", "6:30 PM", "BK-TEST",
                               ["A1", "A2"], ["Asha", "Ravi"])
        out = buf.getvalue()
        for text in ("asha@example.com", "Jawan", "6:30 PM", "BK-TEST", "A1", "Asha", "A2", "Ravi"):
            self.assertIn(text, out)

    def test_every_movie_has_all_its_show_times_in_the_database(self):
        with SessionLocal() as db:
            for m in MOVIES:
                movie = db.query(Movie).filter_by(name=m["name"]).first()
                self.assertIsNotNone(movie)
                times = [s.time for s in db.query(Show).filter_by(movie_id=movie.id)]
                self.assertEqual(sorted(times), sorted(m["times"]))

    def test_food_is_saved_with_the_booking_and_removed_when_cancelled(self):
        result = book_seats(1, "A7", 1, ["Asha"],
                            [{"id": FOOD[0]["id"], "qty": 2}, {"id": FOOD[1]["id"], "qty": 0}])
        self.assertEqual(len(result["food"]), 1)   # the quantity-0 item is ignored
        self.assertEqual(result["food_total"], 2 * FOOD[0]["price"])
        with SessionLocal() as db:
            self.assertEqual(db.query(FoodOrder).filter_by(ref=result["ref"]).count(), 1)
        cancel_booking(result["ref"], 1)
        with SessionLocal() as db:
            self.assertEqual(db.query(FoodOrder).filter_by(ref=result["ref"]).count(), 0)

    def test_bad_food_or_taken_seat_saves_nothing(self):
        with SessionLocal() as db:
            before = db.query(FoodOrder).count()
        with self.assertRaises(ValueError):
            book_seats(1, "A8", 1, ["Asha"], [{"id": "no-such-item", "qty": 1}])
        with self.assertRaises(ValueError):
            book_seats(1, "A8", 1, ["Asha"], [{"id": FOOD[0]["id"], "qty": MAX_FOOD_QTY + 1}])
        book_seats(1, "A8", 2, ["Ravi"])   # A8 was still free, so nothing was kept by the failures above
        with self.assertRaises(SeatTaken):   # A8 is taken now, so the food must not be saved either
            book_seats(1, "A8", 3, ["Meera"], [{"id": FOOD[0]["id"], "qty": 1}])
        with SessionLocal() as db:
            self.assertEqual(db.query(FoodOrder).count(), before)

    def test_confirmation_email_lists_the_food(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            send_booking_email("asha@example.com", "asha", "Jawan", "6:30 PM", "BK-FOOD", ["A1"], ["Asha"],
                               [{"name": "Nachos with Cheese", "qty": 2, "price": 180}])
        out = buf.getvalue()
        for text in ("Nachos with Cheese", "Food total: Rs. 360"):
            self.assertIn(text, out)

    # ---- reviews and the review email (user ids 9001+ are used only by these tests)
    def _make_user(self, uid):
        with SessionLocal() as db:
            db.add(User(id=uid, username=f"rev{uid}", email=f"rev{uid}@example.com", phone=str(uid),
                        salt="00" * 16, pw_hash="x"))
            db.commit()

    def test_review_email_comes_after_the_delay_and_only_once(self):
        import contextlib
        import io
        self._make_user(9001)
        book_seats(2, "A5", 9001, ["Rev One"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            send_due_review_emails(now=time.time())                              # too early: nothing is sent
            send_due_review_emails(now=time.time() + REVIEW_DELAY_SECONDS + 1)   # due: sent
            send_due_review_emails(now=time.time() + REVIEW_DELAY_SECONDS + 99)  # already sent: not again
        out = buf.getvalue()
        self.assertEqual(out.count("To: rev9001@example.com"), 1)
        self.assertIn(f"/?review=1", out)            # the link opens the review box of movie 1 (show 2 belongs to movie 1)

    def test_cancelled_booking_gets_no_review_email(self):
        import contextlib
        import io
        self._make_user(9002)
        ref = book_seats(2, "A6", 9002, ["Rev Two"])["ref"]
        cancel_booking(ref, 9002)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            send_due_review_emails(now=time.time() + REVIEW_DELAY_SECONDS + 1)
        self.assertNotIn("rev9002@example.com", buf.getvalue())
        self.assertFalse(can_review(9002, 1))

    def test_only_people_who_booked_can_review_and_each_person_gets_one_review(self):
        self._make_user(9003)
        with self.assertRaises(ReviewNotAllowed):
            save_review(9003, 1, 5, "never booked")
        book_seats(3, "D1", 9003, ["Rev Three"])
        save_review(9003, 1, 4, "good")
        save_review(9003, 1, 5, "even better after a second look")   # replaces the first one
        mine = [r for r in get_reviews(1, 9003)["reviews"] if r["mine"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["rating"], mine[0]["comment"]), (5, "even better after a second look"))
        for bad in (0, 6, True, "5"):
            with self.assertRaises(ValueError):
                save_review(9003, 1, bad, "x")
        with self.assertRaises(ValueError):
            save_review(9003, 1, 5, "x" * (MAX_REVIEW_CHARS + 1))

    def test_reviews_belong_to_their_own_movie_and_average_is_correct(self):
        self._make_user(9004)
        self._make_user(9005)
        book_seats(6, "E1", 9004, ["A"])    # show 6 belongs to movie 2
        book_seats(6, "E2", 9005, ["B"])
        save_review(9004, 2, 5, "loved it")
        save_review(9005, 2, 4, "nice")
        data = get_reviews(2, 9004)
        self.assertEqual((data["count"], data["average"]), (2, 4.5))
        self.assertEqual(rating_summary()[2], (4.5, 2))
        self.assertFalse(any(r["comment"] == "loved it" for r in get_reviews(3, 9004)["reviews"]))   # other movies don't show it

    # ---- payments (user ids 9101+ and show ids 7-9 are used only here)
    def test_seats_are_held_until_paid_and_the_server_works_out_the_price(self):
        held = hold_seats(7, "A1", 9101, ["Pay One", "Pay Two"], [{"id": FOOD[0]["id"], "qty": 1}])
        self.assertEqual(held["amount"], 2 * ticket_price(7) + FOOD[0]["price"])
        with SessionLocal() as db:
            self.assertEqual(db.query(PaymentOrder).filter_by(ref=held["ref"]).one().status, "pending")
            self.assertEqual(db.query(ReviewInvite).filter_by(ref=held["ref"]).count(), 0)   # no review invite before payment
        with self.assertRaises(SeatTaken):
            hold_seats(7, "A2", 9102, ["Someone else"])   # a held seat cannot be taken by others
        self.assertEqual(confirm_payment(held["ref"], "pay_1"), "confirmed")
        self.assertEqual(confirm_payment(held["ref"], "pay_1"), "already")   # a repeat (e.g. the webhook) changes nothing
        with SessionLocal() as db:
            self.assertEqual(db.query(ReviewInvite).filter_by(ref=held["ref"]).count(), 1)
            self.assertEqual(db.query(PaymentOrder).filter_by(ref=held["ref"]).one().payment_id, "pay_1")

    def test_unpaid_hold_expires_and_frees_the_seats(self):
        held = hold_seats(7, "B1", 9103, ["Late"])
        self.assertEqual(release_expired_holds(now=time.time()), 0)   # still inside the payment window
        self.assertGreaterEqual(release_expired_holds(now=time.time() + PAYMENT_HOLD_SECONDS + 1), 1)
        with SessionLocal() as db:
            self.assertEqual(db.query(PaymentOrder).filter_by(ref=held["ref"]).one().status, "expired")
            self.assertEqual(db.query(Ticket).filter_by(ref=held["ref"]).count(), 0)
        book_seats(7, "B1", 9104, ["Next person"])   # the seat is free for someone else
        # the first customer pays after all, but the seat is gone: the money must be flagged for refund
        self.assertEqual(confirm_payment(held["ref"], "pay_late"), "refund_needed")

    def test_payment_that_arrives_late_still_books_if_the_seats_are_free(self):
        held = hold_seats(7, "B3", 9105, ["Slow payer"])
        release_expired_holds(now=time.time() + PAYMENT_HOLD_SECONDS + 1)
        self.assertEqual(confirm_payment(held["ref"], "pay_slow"), "confirmed")   # same seats were still free
        with SessionLocal() as db:
            self.assertEqual(sorted(t.seat for t in db.query(Ticket).filter_by(ref=held["ref"])), ["B3"])

    def test_cancelling_an_unpaid_hold_releases_it_without_a_refund(self):
        held = hold_seats(7, "C1", 9106, ["Changed mind"])
        self.assertEqual(cancel_booking(held["ref"], 9106), {"refunded": 0})
        book_seats(7, "C1", 9107, ["Next"])

    def test_payment_signature_check(self):
        import hashlib as hl
        import hmac as hm
        saved = globals()["RAZORPAY_KEY_SECRET"]
        try:
            globals()["RAZORPAY_KEY_SECRET"] = "test_secret"
            good = hm.new(b"test_secret", b"order_1|pay_1", hl.sha256).hexdigest()
            self.assertTrue(verify_signature("order_1", "pay_1", good))
            self.assertFalse(verify_signature("order_1", "pay_2", good))      # a different payment
            self.assertFalse(verify_signature("order_2", "pay_1", good))      # a different order
            self.assertFalse(verify_signature("order_1", "pay_1", "0" * 64))  # a made-up signature
            self.assertFalse(verify_signature("order_1", "pay_1", ""))
            globals()["RAZORPAY_KEY_SECRET"] = ""                              # no secret configured: nothing verifies
            self.assertFalse(verify_signature("order_1", "pay_1", hm.new(b"", b"order_1|pay_1", hl.sha256).hexdigest()))
        finally:
            globals()["RAZORPAY_KEY_SECRET"] = saved

    def test_razorpay_order_amount_and_refund_on_cancel(self):
        calls = []

        def fake_razorpay(path, payload):
            calls.append((path, payload))
            return {"id": "order_fake1"} if path == "/orders" else {"id": "rfnd_1"}
        saved = {k: globals()[k] for k in ("PAYMENT_MODE", "razorpay_request")}
        try:
            globals()["PAYMENT_MODE"], globals()["razorpay_request"] = "razorpay", fake_razorpay
            held = hold_seats(8, "A1", 9108, ["Rz"])
            self.assertEqual(create_provider_order(held["ref"], held["amount"]), "order_fake1")
            self.assertEqual(calls[0], ("/orders", {"amount": held["amount"] * 100, "currency": "INR", "receipt": held["ref"]}))
            confirm_payment(held["ref"], "pay_rz1")
            # a refund the provider refuses must leave the booking untouched
            globals()["razorpay_request"] = lambda path, payload: (_ for _ in ()).throw(PaymentError("simulated outage"))
            with self.assertRaises(PaymentError):
                cancel_booking(held["ref"], 9108)
            with SessionLocal() as db:
                self.assertEqual(db.query(Ticket).filter_by(ref=held["ref"]).count(), 1)
                self.assertEqual(db.query(PaymentOrder).filter_by(ref=held["ref"]).one().status, "paid")
            globals()["razorpay_request"] = fake_razorpay
            self.assertEqual(cancel_booking(held["ref"], 9108), {"refunded": held["amount"]})
            self.assertEqual(calls[-1], ("/payments/pay_rz1/refund", {"amount": held["amount"] * 100, "notes": {"booking": held["ref"]}}))
            with SessionLocal() as db:
                self.assertEqual(db.query(PaymentOrder).filter_by(ref=held["ref"]).one().status, "refunded")
        finally:
            globals().update(saved)

    def test_demo_payment_endpoint_is_off_when_real_keys_are_set(self):
        saved = globals()["PAYMENT_MODE"]
        try:
            globals()["PAYMENT_MODE"] = "razorpay"
            with self.assertRaises(HTTPException) as ctx:
                demo_payment(DemoPayIn(ref="BK-X"), BackgroundTasks(), 1)
            self.assertEqual(ctx.exception.status_code, 404)
        finally:
            globals()["PAYMENT_MODE"] = saved

    def test_webhook_confirms_a_booking_once_and_rejects_bad_signatures(self):
        import hashlib as hl
        import hmac as hm
        held = hold_seats(8, "B1", 9109, ["Hook"])
        with SessionLocal() as db:
            db.query(PaymentOrder).filter_by(ref=held["ref"]).update({"provider_order_id": "order_hook1"})
            db.commit()
        body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_hook1", "order_id": "order_hook1"}}}}).encode()
        saved = globals()["RAZORPAY_WEBHOOK_SECRET"]
        try:
            globals()["RAZORPAY_WEBHOOK_SECRET"] = ""
            self.assertFalse(process_webhook(body, "anything", BackgroundTasks()))   # switched off without a secret
            globals()["RAZORPAY_WEBHOOK_SECRET"] = "whsec"
            self.assertFalse(process_webhook(body, "0" * 64, BackgroundTasks()))
            good = hm.new(b"whsec", body, hl.sha256).hexdigest()
            self.assertTrue(process_webhook(body, good, BackgroundTasks()))
            self.assertTrue(process_webhook(body, good, BackgroundTasks()))          # repeated delivery is harmless
        finally:
            globals()["RAZORPAY_WEBHOOK_SECRET"] = saved
        with SessionLocal() as db:
            order = db.query(PaymentOrder).filter_by(ref=held["ref"]).one()
            self.assertEqual((order.status, order.payment_id), ("paid", "pay_hook1"))
            self.assertEqual(db.query(ReviewInvite).filter_by(ref=held["ref"]).count(), 1)

    # ---- the waiting line (user ids 9201+, show 9)
    def test_second_person_waits_and_gets_the_seat_when_the_first_cancels(self):
        first = request_seats(9, 9201, "A1", ["First"])
        self.assertEqual(first["type"], "hold")
        second = request_seats(9, 9202, "A1", ["Second"])
        self.assertEqual(second["type"], "queued")
        process_waitlist()
        with SessionLocal() as db:
            self.assertEqual(db.get(WaitlistEntry, second["id"]).status, "waiting")   # still held by the first person
        cancel_booking(first["ref"], 9201)   # the first person cancels at the payment stage
        process_waitlist()
        with SessionLocal() as db:
            entry = db.get(WaitlistEntry, second["id"])
            self.assertEqual(entry.status, "ready")   # now THEIR payment window is open
            order = db.query(PaymentOrder).filter_by(ref=entry.ref).one()
            self.assertEqual((order.user_id, order.status), (9202, "pending"))
        self.assertEqual(confirm_payment(entry.ref, "pay_second"), "confirmed")

    def test_waiting_person_gets_the_seat_when_the_first_one_times_out(self):
        first = request_seats(9, 9203, "B1", ["First"])
        second = request_seats(9, 9204, "B1", ["Second"])
        process_waitlist(now=time.time() + PAYMENT_HOLD_SECONDS + 1)   # the first person never paid
        with SessionLocal() as db:
            self.assertEqual(db.get(WaitlistEntry, second["id"]).status, "ready")
            self.assertEqual(db.query(PaymentOrder).filter_by(ref=first["ref"]).one().status, "expired")

    def test_waiting_person_is_told_when_the_first_one_pays(self):
        first = request_seats(9, 9205, "C1", ["First"])
        second = request_seats(9, 9206, "C1", ["Second"])
        confirm_payment(first["ref"], "pay_first")
        process_waitlist()
        with SessionLocal() as db:
            entry = db.get(WaitlistEntry, second["id"])
            self.assertEqual(entry.status, "failed")
            self.assertIn("booked", entry.message)
        with self.assertRaises(SeatTaken):   # a seat that is paid for cannot be queued for
            request_seats(9, 9207, "C1", ["Third"])

    def test_the_line_is_first_come_first_served_and_nobody_jumps_it(self):
        first = request_seats(9, 9208, "D1", ["First"])
        second = request_seats(9, 9209, "D1", ["Second"])
        third = request_seats(9, 9210, "D1", ["Third"])
        self.assertEqual((second["type"], third["type"]), ("queued", "queued"))
        cancel_booking(first["ref"], 9208)
        newcomer = request_seats(9, 9211, "D1", ["Newcomer"])   # the seat is free right now, but two people are ahead
        with SessionLocal() as db:
            self.assertEqual(db.get(WaitlistEntry, second["id"]).status, "ready")   # the oldest waiter got it
            self.assertEqual(db.get(WaitlistEntry, third["id"]).status, "waiting")
        self.assertEqual(newcomer["type"], "queued")

    def test_leaving_the_line_and_one_line_per_show(self):
        first = request_seats(9, 9212, "E1", ["First"])
        waiting = request_seats(9, 9213, "E1", ["Waiter"])
        with self.assertRaises(ValueError):
            join_waitlist(9, 9213, "E1", ["Again"])   # already in the line for this show
        leave_waitlist(waiting["id"], 9213)
        cancel_booking(first["ref"], 9212)
        process_waitlist()
        with SessionLocal() as db:
            self.assertEqual(db.get(WaitlistEntry, waiting["id"]).status, "cancelled")   # a person who left gets nothing
        book_seats(9, "E1", 9214, ["Someone else"])

    def test_every_movie_has_poster_data(self):
        import datetime
        for m in MOVIES:
            self.assertTrue(m["genre"] and m["emoji"] and len(m["colors"]) == 2 and m["times"])
            self.assertTrue(isinstance(m["price"], int) and m["price"] > 0)   # ticket price in rupees
            datetime.date.fromisoformat(m["release"])   # must be a valid YYYY-MM-DD date
        self.assertEqual(len({m["name"] for m in MOVIES}), len(MOVIES))   # no duplicate names

    def test_frontend_file_exists(self):
        self.assertTrue(FRONTEND_FILE.exists(), "index.html must be in the same folder as booking.py")


if __name__ == "__main__":
    if "--test" in sys.argv:
        unittest.main(argv=[sys.argv[0]], verbosity=2)
    else:
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=8000)
