import os
import sqlite3
import stripe
from flask import Flask, render_template, request, redirect, session, g, flash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "ridetravay_phase2_secret"

DATABASE = "database.db"
WEEKLY_PRICE = 80
PLATFORM_FEE = 6
DRIVER_PAYOUT = 74
MAX_PASSENGERS = 4

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def add_column_if_missing(table_name, column_name, definition):
    db = get_db()
    columns = [row["name"] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()]
    if column_name not in columns:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
        db.commit()


def init_db():
    db = get_db()
    cur = db.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT,
        full_name TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        city TEXT DEFAULT '',
        work_address TEXT DEFAULT '',
        bank_name TEXT DEFAULT '',
        account_holder TEXT DEFAULT '',
        account_number TEXT DEFAULT '',
        routing_number TEXT DEFAULT '',
        stripe_account_id TEXT DEFAULT '',
        payouts_enabled INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER,
        work_address TEXT,
        home_city TEXT,
        pickup_time TEXT,
        schedule TEXT,
        driver_phone TEXT,
        seats INTEGER DEFAULT 4,
        status TEXT DEFAULT 'active'
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        passenger_id INTEGER,
        ride_id INTEGER,
        status TEXT,
        start_date TEXT,
        next_payment_date TEXT,
        weekly_price INTEGER DEFAULT 80,
        stripe_customer_id TEXT DEFAULT '',
        stripe_subscription_id TEXT DEFAULT '',
        stripe_checkout_session_id TEXT DEFAULT ''
    )
    """)

    db.commit()

    add_column_if_missing("users", "bank_name", "TEXT DEFAULT ''")
    add_column_if_missing("users", "account_holder", "TEXT DEFAULT ''")
    add_column_if_missing("users", "account_number", "TEXT DEFAULT ''")
    add_column_if_missing("users", "routing_number", "TEXT DEFAULT ''")
    add_column_if_missing("users", "stripe_account_id", "TEXT DEFAULT ''")
    add_column_if_missing("users", "payouts_enabled", "INTEGER DEFAULT 0")
    add_column_if_missing("rides", "schedule", "TEXT DEFAULT ''")
    add_column_if_missing("rides", "driver_phone", "TEXT DEFAULT ''")
    add_column_if_missing("rides", "status", "TEXT DEFAULT 'active'")
    add_column_if_missing("subscriptions", "weekly_price", "INTEGER DEFAULT 80")
    add_column_if_missing("subscriptions", "stripe_customer_id", "TEXT DEFAULT ''")
    add_column_if_missing("subscriptions", "stripe_subscription_id", "TEXT DEFAULT ''")
    add_column_if_missing("subscriptions", "stripe_checkout_session_id", "TEXT DEFAULT ''")


@app.before_request
def before_request():
    init_db()


def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute(
        "SELECT * FROM users WHERE id=?",
        (session["user_id"],)
    ).fetchone()


def require_login():
    return "user_id" in session


def mask_account(account_number):
    if not account_number:
        return ""
    s = str(account_number)
    if len(s) <= 4:
        return s
    return "*" * (len(s) - 4) + s[-4:]


def get_active_subscription_for_passenger(passenger_id):
    db = get_db()
    return db.execute("""
        SELECT *
        FROM subscriptions
        WHERE passenger_id=?
          AND status IN ('checkout_started', 'first_week_free', 'active_paid', 'cancel_pending')
        ORDER BY id DESC
        LIMIT 1
    """, (passenger_id,)).fetchone()


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "mask_account": mask_account,
        "WEEKLY_PRICE": WEEKLY_PRICE,
        "PLATFORM_FEE": PLATFORM_FEE,
        "DRIVER_PAYOUT": DRIVER_PAYOUT,
    }


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, password, role, full_name, phone) VALUES (?, ?, ?, ?, ?)",
                (
                    request.form["username"].strip(),
                    request.form["password"].strip(),
                    request.form["role"],
                    request.form["full_name"].strip(),
                    request.form["phone"].strip(),
                )
            )
            db.commit()
            flash("Kont ou kreye avèk siksè.")
            return redirect("/login")
        except sqlite3.IntegrityError:
            flash("Non itilizatè sa deja egziste.")
            return redirect("/register")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = get_db().execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (request.form["username"].strip(), request.form["password"].strip())
        ).fetchone()

        if user:
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            if user["username"] == "kervens2004":
                return redirect("/admin-ridetravay-control")
            return redirect("/passenger" if user["role"] == "passenger" else "/driver")

        flash("Login pa bon.")
        return redirect("/login")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/passenger")
def passenger():
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["role"] != "passenger":
        return redirect("/driver")

    db = get_db()

    filters = {
        "work_address": request.args.get("work_address", "").strip(),
        "home_city": request.args.get("home_city", "").strip(),
        "pickup_time": request.args.get("pickup_time", "").strip(),
        "schedule": request.args.get("schedule", "").strip(),
    }

    query = """
    SELECT rides.*, users.full_name AS driver_name
    FROM rides
    LEFT JOIN users ON users.id = rides.driver_id
    WHERE rides.status='active' AND rides.seats > 0
    """
    params = []

    if filters["work_address"]:
        query += " AND lower(rides.work_address) LIKE ?"
        params.append(f"%{filters['work_address'].lower()}%")
    if filters["home_city"]:
        query += " AND lower(rides.home_city) LIKE ?"
        params.append(f"%{filters['home_city'].lower()}%")
    if filters["pickup_time"]:
        query += " AND lower(rides.pickup_time) LIKE ?"
        params.append(f"%{filters['pickup_time'].lower()}%")
    if filters["schedule"]:
        query += " AND lower(rides.schedule) LIKE ?"
        params.append(f"%{filters['schedule'].lower()}%")

    query += " ORDER BY rides.id DESC"
    rides = db.execute(query, tuple(params)).fetchall()

    active_sub = db.execute("""
        SELECT subscriptions.*, rides.work_address, rides.home_city, rides.pickup_time, rides.schedule,
               rides.driver_phone, users.full_name AS driver_name
        FROM subscriptions
        JOIN rides ON rides.id = subscriptions.ride_id
        LEFT JOIN users ON users.id = rides.driver_id
        WHERE subscriptions.passenger_id=? AND subscriptions.status!='cancelled'
        ORDER BY subscriptions.id DESC LIMIT 1
    """, (user["id"],)).fetchone()

    return render_template(
        "passenger.html",
        rides=rides,
        active_sub=active_sub,
        filters=filters
    )


@app.route("/create-checkout-session/<int:ride_id>", methods=["POST"])
def create_checkout_session(ride_id):
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["role"] != "passenger":
        return redirect("/driver")

    db = get_db()

    ride = db.execute("""
        SELECT rides.*, users.full_name AS driver_name, users.stripe_account_id
        FROM rides
        LEFT JOIN users ON users.id = rides.driver_id
        WHERE rides.id=? AND rides.status='active'
    """, (ride_id,)).fetchone()

    if not ride or ride["seats"] <= 0:
        flash("Ride sa a pa disponib ankò.")
        return redirect("/passenger")

    existing = get_active_subscription_for_passenger(user["id"])
    if existing:
        flash("Ou deja gen yon plan aktif oswa checkout deja kòmanse.")
        return redirect("/passenger")

    driver_stripe_account = (ride["stripe_account_id"] or "").strip()
    if not driver_stripe_account:
        flash("Chofè sa a poko konekte Stripe li.")
        return redirect("/passenger")

    if not STRIPE_PRICE_ID:
        flash("Stripe price id la pa konfigire.")
        return redirect("/passenger")

    checkout_session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[
            {
                "price": STRIPE_PRICE_ID,
                "quantity": 1,
            }
        ],
        metadata={
            "ride_id": str(ride_id),
            "passenger_id": str(user["id"]),
            "driver_id": str(ride["driver_id"]),
        },
        subscription_data={
            "trial_period_days": 7,
            "application_fee_percent": 7.5,
            "transfer_data": {
                "destination": driver_stripe_account
            },
            "metadata": {
                "ride_id": str(ride_id),
                "passenger_id": str(user["id"]),
                "driver_id": str(ride["driver_id"]),
            },
        },
        success_url=f"{APP_BASE_URL}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/checkout-cancel",
    )

    db.execute("""
        INSERT INTO subscriptions
        (passenger_id, ride_id, status, start_date, next_payment_date, weekly_price, stripe_checkout_session_id)
        VALUES (?, ?, ?, date('now'), date('now', '+7 day'), ?, ?)
    """, (
        user["id"],
        ride_id,
        "checkout_started",
        WEEKLY_PRICE,
        checkout_session.id,
    ))
    db.commit()

    return redirect(checkout_session.url, code=303)


@app.route("/checkout-success")
def checkout_success():
    session_id = request.args.get("session_id")
    return render_template("checkout_success.html", session_id=session_id)


@app.route("/checkout-cancel")
def checkout_cancel():
    flash("Checkout la pa fini.")
    return redirect("/passenger")


@app.route("/webhook", methods=["POST"])
@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print("Webhook signature error:", e)
        return "Invalid webhook", 400

    db = get_db()

    try:

        event_type = event.get("type")
        obj = event.get("data", {}).get("object", {})

        print("Webhook received:", event_type)

        # ======================
        # CHECKOUT COMPLETED
        # ======================

        if event_type == "checkout.session.completed":

            metadata = obj.get("metadata", {})

            passenger_id = metadata.get("passenger_id")
            ride_id = metadata.get("ride_id")

            stripe_customer_id = obj.get("customer")
            stripe_subscription_id = obj.get("subscription")
            session_id = obj.get("id")

            if passenger_id and ride_id and session_id:

                passenger_id = int(passenger_id)
                ride_id = int(ride_id)

                ride = db.execute(
                    "SELECT * FROM rides WHERE id=? AND status='active'",
                    (ride_id,)
                ).fetchone()

                sub = db.execute("""
                    SELECT *
                    FROM subscriptions
                    WHERE passenger_id=? 
                    AND stripe_checkout_session_id=?
                    ORDER BY id DESC
                    LIMIT 1
                """, (passenger_id, session_id)).fetchone()

                if ride and sub:

                    db.execute(
                        "UPDATE rides SET seats = seats - 1 WHERE id=?",
                        (ride_id,)
                    )

                    db.execute("""
                        UPDATE subscriptions
                        SET status='first_week_free',
                            stripe_customer_id=?,
                            stripe_subscription_id=?,
                            start_date=date('now'),
                            next_payment_date=date('now', '+7 day')
                        WHERE id=?
                    """, (
                        stripe_customer_id or "",
                        stripe_subscription_id or "",
                        sub["id"],
                    ))

                    db.commit()


        # ======================
        # WEEKLY PAYMENT SUCCESS
        # ======================

        elif event_type == "invoice.payment_succeeded":

            stripe_subscription_id = obj.get("subscription")

            if stripe_subscription_id:

                db.execute("""
                    UPDATE subscriptions
                    SET status='active_paid',
                        next_payment_date=date(next_payment_date, '+7 day')
                    WHERE stripe_subscription_id=?
                """, (stripe_subscription_id,))

                db.commit()


        # ======================
        # SUBSCRIPTION UPDATED
        # ======================

        elif event_type == "customer.subscription.updated":

            stripe_subscription_id = obj.get("id")
            cancel_at_period_end = obj.get("cancel_at_period_end", False)

            db.execute("""
                UPDATE subscriptions
                SET status=?
                WHERE stripe_subscription_id=?
            """, (
                "cancel_pending" if cancel_at_period_end else "active_paid",
                stripe_subscription_id,
            ))

            db.commit()


        # ======================
        # SUBSCRIPTION CANCELLED
        # ======================

        elif event_type == "customer.subscription.deleted":

            stripe_subscription_id = obj.get("id")

            sub = db.execute("""
                SELECT *
                FROM subscriptions
                WHERE stripe_subscription_id=?
                ORDER BY id DESC
                LIMIT 1
            """, (stripe_subscription_id,)).fetchone()

            if sub:

                db.execute(
                    "UPDATE subscriptions SET status='cancelled' WHERE id=?",
                    (sub["id"],)
                )

                db.execute(
                    "UPDATE rides SET seats = seats + 1 WHERE id=?",
                    (sub["ride_id"],)
                )

                db.commit()

    except Exception as e:

        print("Webhook processing crash:", e)
        return "Webhook error", 500


    return "ok", 200
@app.route("/stripe-cancel-subscription")
def stripe_cancel_subscription():
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["role"] != "passenger":
        return redirect("/driver")

    db = get_db()
    sub = db.execute("""
        SELECT * FROM subscriptions
        WHERE passenger_id=?
          AND stripe_subscription_id != ''
          AND status IN ('first_week_free', 'active_paid')
        ORDER BY id DESC
        LIMIT 1
    """, (user["id"],)).fetchone()

    if not sub:
        flash("Pa gen abònman Stripe aktif.")
        return redirect("/passenger")

    try:
        stripe.Subscription.modify(
            sub["stripe_subscription_id"],
            cancel_at_period_end=True
        )
    except Exception as e:
        flash(f"Stripe error: {e}")
        return redirect("/passenger")

    db.execute("""
        UPDATE subscriptions
        SET status='cancel_pending'
        WHERE id=?
    """, (sub["id"],))
    db.commit()

    flash("Abònman an ap fini nan fen peryòd aktyèl la.")
    return redirect("/passenger")


@app.route("/passenger/profile", methods=["GET", "POST"])
def passenger_profile():
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["role"] != "passenger":
        return redirect("/driver")

    if request.method == "POST":
        db = get_db()
        db.execute("""
            UPDATE users
            SET full_name=?, phone=?, city=?, work_address=?
            WHERE id=?
        """, (
            request.form["full_name"].strip(),
            request.form["phone"].strip(),
            request.form["city"].strip(),
            request.form["work_address"].strip(),
            user["id"],
        ))
        db.commit()
        flash("Profil pasajè a sove.")
        return redirect("/passenger/profile")

    return render_template("passenger_profile.html", user=user)


@app.route("/driver", methods=["GET", "POST"])
def driver():
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["role"] != "driver":
        return redirect("/passenger")

    db = get_db()

    if request.method == "POST":
        work = request.form["work"].strip()
        home = request.form["home"].strip()
        pickup_time = request.form["pickup_time"].strip()
        schedule = request.form["schedule"].strip()
        driver_phone = request.form["driver_phone"].strip()

        if work and home and pickup_time and schedule and driver_phone:
            db.execute("""
                INSERT INTO rides (driver_id, work_address, home_city, pickup_time, schedule, driver_phone, seats, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
            """, (user["id"], work, home, pickup_time, schedule, driver_phone, MAX_PASSENGERS))
            db.commit()
            flash("Ride la ajoute.")

    rides = db.execute("""
        SELECT rides.*,
        (SELECT COUNT(*) FROM subscriptions
         WHERE subscriptions.ride_id = rides.id
         AND subscriptions.status != 'cancelled') AS passenger_count
        FROM rides
        WHERE rides.driver_id=? AND rides.status='active'
        ORDER BY rides.id DESC
    """, (user["id"],)).fetchall()

    passengers_by_ride = {}
    total_passengers = 0
    total_driver_earnings = 0
    total_platform_fees = 0

    for ride in rides:
        passengers = db.execute("""
            SELECT users.full_name, users.phone, subscriptions.status
            FROM subscriptions
            JOIN users ON users.id = subscriptions.passenger_id
            WHERE subscriptions.ride_id=? AND subscriptions.status != 'cancelled'
            ORDER BY subscriptions.id DESC
        """, (ride["id"],)).fetchall()

        passengers_by_ride[ride["id"]] = passengers

        count = len(passengers)
        total_passengers += count
        total_driver_earnings += count * DRIVER_PAYOUT
        total_platform_fees += count * PLATFORM_FEE

    return render_template(
        "driver.html",
        rides=rides,
        passengers_by_ride=passengers_by_ride,
        total_passengers=total_passengers,
        total_driver_earnings=total_driver_earnings,
        total_platform_fees=total_platform_fees,
    )


@app.route("/driver/profile", methods=["GET", "POST"])
def driver_profile():
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["role"] != "driver":
        return redirect("/passenger")

    if request.method == "POST":
        db = get_db()
        db.execute("""
            UPDATE users
            SET full_name=?, phone=?, city=?, work_address=?, bank_name=?, account_holder=?, account_number=?, routing_number=?
            WHERE id=?
        """, (
            request.form["full_name"].strip(),
            request.form["phone"].strip(),
            request.form["city"].strip(),
            request.form["work_address"].strip(),
            request.form.get("bank_name", "").strip(),
            request.form.get("account_holder", "").strip(),
            request.form.get("account_number", "").strip(),
            request.form.get("routing_number", "").strip(),
            user["id"],
        ))
        db.commit()
        flash("Profil chofè a sove.")
        return redirect("/driver/profile")

    return render_template("driver_profile.html", user=user)


@app.route("/driver/connect-stripe")
def connect_stripe():
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["role"] != "driver":
        return redirect("/passenger")

    if not stripe.api_key:
        flash("Stripe pa konfigire.")
        return redirect("/driver")

    db = get_db()

    try:
        if not user["stripe_account_id"]:
            account = stripe.Account.create(
                type="express",
                country="US",
                email=user["username"]
            )

            db.execute(
                "UPDATE users SET stripe_account_id=? WHERE id=?",
                (account.id, user["id"])
            )
            db.commit()
            account_id = account.id
        else:
            account_id = user["stripe_account_id"]

        link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=f"{APP_BASE_URL}/driver",
            return_url=f"{APP_BASE_URL}/driver",
            type="account_onboarding",
        )
        return redirect(link.url)
    except Exception as e:
        flash(f"Stripe Connect error: {e}")
        return redirect("/driver")


@app.route("/delete-ride/<int:ride_id>")
def delete_ride(ride_id):
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["role"] != "driver":
        return redirect("/passenger")

    db = get_db()
    db.execute("UPDATE rides SET status='deleted' WHERE id=? AND driver_id=?", (ride_id, user["id"]))
    db.commit()
    flash("Ride la retire.")
    return redirect("/driver")


@app.route("/admin-ridetravay-control")
def admin():
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["username"] != "kervens2004":
        return "<h1>Aksè entèdi</h1>", 403

    db = get_db()

    users = db.execute("""
        SELECT id, username, role, full_name, phone
        FROM users
        ORDER BY id DESC
    """).fetchall()

    rides = db.execute("""
        SELECT rides.*, users.full_name AS driver_name
        FROM rides
        LEFT JOIN users ON users.id = rides.driver_id
        ORDER BY rides.id DESC
    """).fetchall()

    subscriptions = db.execute("""
        SELECT subscriptions.*, users.full_name AS passenger_name, rides.work_address, rides.home_city
        FROM subscriptions
        LEFT JOIN users ON users.id = subscriptions.passenger_id
        LEFT JOIN rides ON rides.id = subscriptions.ride_id
        ORDER BY subscriptions.id DESC
    """).fetchall()

    total_users = len(users)
    total_rides = len(rides)
    total_subscriptions = len(subscriptions)
    active_subscriptions = len([s for s in subscriptions if s["status"] != "cancelled"])

    total_revenue = active_subscriptions * WEEKLY_PRICE
    total_platform_revenue = active_subscriptions * PLATFORM_FEE
    total_driver_payouts = active_subscriptions * DRIVER_PAYOUT

    return render_template(
        "admin.html",
        users=users,
        rides=rides,
        subscriptions=subscriptions,
        total_users=total_users,
        total_rides=total_rides,
        total_subscriptions=total_subscriptions,
        active_subscriptions=active_subscriptions,
        total_revenue=total_revenue,
        total_platform_revenue=total_platform_revenue,
        total_driver_payouts=total_driver_payouts,
    )


@app.route("/admin/delete-user/<int:user_id>", methods=["POST"])
def admin_delete_user(user_id):
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["username"] != "kervens2004":
        return "<h1>Aksè entèdi</h1>", 403

    db = get_db()

    if user["id"] == user_id:
        flash("Ou pa ka efase pwòp kont admin ou.")
        return redirect("/admin-ridetravay-control")

    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    flash("Itilizatè a efase.")
    return redirect("/admin-ridetravay-control")


@app.route("/admin/delete-ride/<int:ride_id>", methods=["POST"])
def admin_delete_ride(ride_id):
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["username"] != "kervens2004":
        return "<h1>Aksè entèdi</h1>", 403

    db = get_db()
    db.execute("UPDATE rides SET status='deleted' WHERE id=?", (ride_id,))
    db.commit()
    flash("Ride la retire pa admin.")
    return redirect("/admin-ridetravay-control")


@app.route("/admin/cancel-subscription/<int:sub_id>", methods=["POST"])
def admin_cancel_subscription(sub_id):
    if not require_login():
        return redirect("/login")

    user = current_user()
    if user["username"] != "kervens2004":
        return "<h1>Aksè entèdi</h1>", 403

    db = get_db()

    sub = db.execute(
        "SELECT * FROM subscriptions WHERE id=?",
        (sub_id,)
    ).fetchone()

    if not sub:
        flash("Abònman an pa egziste.")
        return redirect("/admin-ridetravay-control")

    db.execute(
        "UPDATE subscriptions SET status='cancelled' WHERE id=?",
        (sub_id,)
    )

    db.execute(
        "UPDATE rides SET seats = seats + 1 WHERE id=?",
        (sub["ride_id"],)
    )

    db.commit()
    flash("Abònman an anile pa admin.")
    return redirect("/admin-ridetravay-control")


@app.route("/plan-active")
def plan_active():
    return render_template("plan_active.html")


@app.route("/connect-driver")
def connect_driver():
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

    account = stripe.Account.create(type="express")

    account_link = stripe.AccountLink.create(
        account=account.id,
        refresh_url=os.environ.get("APP_BASE_URL"),
        return_url=os.environ.get("APP_BASE_URL"),
        type="account_onboarding",
    )

    return redirect(account_link.url)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
