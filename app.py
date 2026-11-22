import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

PHONE_RE = re.compile(r"^(09\d{9}|\+639\d{9})$")
VEHICLES = ["Motorcycle", "Bicycle", "E-bike", "Tricycle"]

# Placeholder delivery fees. The admin can add barangays and edit fees at any time
# from Admin > Delivery Fees. Brgy. 11 is PHP 40.00 to match the sample computation.
URBAN_FEE = 40.0
RURAL_FEE = 80.0
RURAL_BARANGAYS = [
    "Antonio Luna", "Bay-ang", "Bayabas", "Caasinan", "Cabinet", "Calamba",
    "Calibunan", "Comagascas", "Concepcion", "Del Pilar", "Katugasan",
    "Kauswagan", "La Fortuna", "Mabini", "Mahaba", "Maporong", "Paraiso",
]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
class JsonRepository:
    """A list of dict records stored in one JSON file."""

    def __init__(self, filename, default):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = os.path.join(DATA_DIR, filename)
        self.lock = threading.RLock()
        if not os.path.exists(self.path):
            self._dump(default)

    def _dump(self, data):
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    def _load(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def all(self):
        with self.lock:
            return self._load()

    def get(self, rid):
        return next((r for r in self.all() if r["id"] == rid), None)

    def add(self, record):
        with self.lock:
            items = self._load()
            record["id"] = max((i["id"] for i in items), default=0) + 1
            items.append(record)
            self._dump(items)
            return record

    def update(self, rid, **fields):
        with self.lock:
            items = self._load()
            for item in items:
                if item["id"] == rid:
                    item.update(fields)
                    self._dump(items)
                    return item
        return None


class AdminAccount(JsonRepository):
    """The admin logs in with a password only (stored hashed)."""

    def __init__(self):
        initial = os.environ.get("DALI_ADMIN_PASSWORD", "admin123")
        super().__init__("admin.json", {"password_hash": generate_password_hash(initial)})

    def verify(self, password):
        return check_password_hash(self._load()["password_hash"], password)


# --------------------------------------------------------------------------
# Accounts (customers and riders)
# --------------------------------------------------------------------------
class AccountService:
    role = ""
    min_age = 13

    def __init__(self, repo):
        self.repo = repo

    def find_by_name(self, name):
        key = (name or "").strip().lower()
        return next((a for a in self.repo.all() if a["full_name"].lower() == key), None)

    def authenticate(self, name, password):
        acct = self.find_by_name(name)
        if acct and check_password_hash(acct["password_hash"], password):
            return acct
        return None

    def validate_extra(self, form):
        return []

    def extra_fields(self, form):
        return {}

    def register(self, form):
        """Return (errors, record). record is None when there are errors."""
        errors = []
        name = " ".join(form.get("full_name", "").split())
        contact = form.get("contact", "").strip().replace(" ", "")
        password = form.get("password", "")

        if len(name) < 3:
            errors.append("Please enter your full name.")
        elif self.find_by_name(name):
            errors.append("That name is already registered. Try logging in instead.")

        try:
            age = int(form.get("age", ""))
            if age < self.min_age or age > 100:
                errors.append(f"Age must be {self.min_age} or older.")
        except ValueError:
            age = None
            errors.append("Age must be a number.")

        if not PHONE_RE.match(contact):
            errors.append("Contact number must look like 09123456789.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != form.get("confirm", ""):
            errors.append("Passwords do not match.")
        errors += self.validate_extra(form)

        if errors:
            return errors, None
        record = {
            "full_name": name,
            "age": age,
            "contact": contact,
            "password_hash": generate_password_hash(password),
            "created_at": now(),
        }
        record.update(self.extra_fields(form))
        return [], self.repo.add(record)


class CustomerService(AccountService):
    role = "user"
    min_age = 13


class RiderService(AccountService):
    role = "rider"
    min_age = 18

    def validate_extra(self, form):
        if form.get("vehicle") not in VEHICLES:
            return ["Please choose a vehicle type."]
        return []

    def extra_fields(self, form):
        return {
            "vehicle": form.get("vehicle"),
            "plate_number": form.get("plate_number", "").strip().upper(),
        }


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
STATUS_LABELS = {
    "pending": "Waiting for rider",
    "accepted": "On the way",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}


class OrderService:
    def __init__(self, orders, barangays):
        self.orders = orders
        self.barangays = barangays

    @staticmethod
    def compute(price, qty, fee):
        subtotal = round(price * qty, 2)
        return subtotal, round(subtotal + fee, 2)

    def create(self, customer, form):
        errors = []
        restaurant = form.get("restaurant", "").strip()
        item = form.get("item", "").strip()
        info = form.get("additional_info", "").strip()
        try:
            price = round(float(form.get("price", "")), 2)
            if price <= 0 or price > 100000:
                raise ValueError
        except ValueError:
            price = 0
            errors.append("Price per item must be a positive number.")
        try:
            qty = int(form.get("quantity", ""))
            if not 1 <= qty <= 99:
                raise ValueError
        except ValueError:
            qty = 0
            errors.append("Quantity must be a whole number from 1 to 99.")
        try:
            brgy = self.barangays.get(int(form.get("barangay", "")))
        except ValueError:
            brgy = None
        if not restaurant:
            errors.append("Restaurant name is required.")
        if not item:
            errors.append("Please enter what you want to order.")
        if not brgy:
            errors.append("Please select your barangay.")
        if errors:
            return errors, None

        subtotal, total = self.compute(price, qty, brgy["fee"])
        order = self.orders.add({
            "user_id": customer["id"],
            "customer_name": customer["full_name"],
            "customer_contact": customer["contact"],
            "restaurant": restaurant,
            "item": item,
            "price": price,
            "quantity": qty,
            "barangay": brgy["name"],
            "additional_info": info,
            "subtotal": subtotal,
            "fee": brgy["fee"],
            "total": total,
            "status": "pending",
            "rider_id": None,
            "rider_name": None,
            "created_at": now(),
            "accepted_at": None,
            "delivered_at": None,
        })
        return [], order

    def cancel(self, order_id, user_id):
        with self.orders.lock:
            o = self.orders.get(order_id)
            if not o or o["user_id"] != user_id or o["status"] != "pending":
                return False
            self.orders.update(order_id, status="cancelled")
            return True

    def accept(self, order_id, rider):
        """A rider can hold one active delivery at a time."""
        with self.orders.lock:
            if any(o["rider_id"] == rider["id"] and o["status"] == "accepted"
                   for o in self.orders.all()):
                return "You already have an active delivery. Finish it first."
            o = self.orders.get(order_id)
            if not o or o["status"] != "pending":
                return "Sorry, that order is no longer available."
            self.orders.update(order_id, status="accepted", rider_id=rider["id"],
                               rider_name=rider["full_name"], accepted_at=now())
            return None

    def deliver(self, order_id, rider):
        with self.orders.lock:
            o = self.orders.get(order_id)
            if not o or o["rider_id"] != rider["id"] or o["status"] != "accepted":
                return False
            self.orders.update(order_id, status="delivered", delivered_at=now())
            return True


# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------
def _default_barangays():
    rows = [{"id": i, "name": f"Brgy. {i}", "fee": URBAN_FEE} for i in range(1, 29)]
    for n, name in enumerate(RURAL_BARANGAYS, start=29):
        rows.append({"id": n, "name": name, "fee": RURAL_FEE})
    return rows


def _secret_key():
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, ".secret")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = _secret_key()

admin_store = AdminAccount()
users_repo = JsonRepository("users.json", [])
riders_repo = JsonRepository("riders.json", [])
orders_repo = JsonRepository("orders.json", [])
barangays_repo = JsonRepository("barangays.json", _default_barangays())

customers = CustomerService(users_repo)
riders = RiderService(riders_repo)
orders = OrderService(orders_repo, barangays_repo)

SERVICES = {"user": customers, "rider": riders}
ROLE_LABELS = {"admin": "Admin", "user": "Customer", "rider": "Rider"}
HOME = {"admin": "admin_dashboard", "user": "user_order", "rider": "rider_dashboard"}


@app.template_filter("peso")
def peso(value):
    return "\u20b1{:,.2f}".format(value or 0)


@app.template_filter("status_label")
def status_label(value):
    return STATUS_LABELS.get(value, value)


@app.context_processor
def inject_globals():
    return {"ROLE_LABELS": ROLE_LABELS}


def login_required(role):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get("role") != role:
                flash("Please log in to continue.", "warn")
                return redirect(url_for("login", role=role))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def current_account():
    return SERVICES[session["role"]].repo.get(session["uid"])


# --------------------------------------------------------------------------
# Public: selection, register, login, logout
# --------------------------------------------------------------------------
@app.route("/")
def index():
    if session.get("role"):
        return redirect(url_for(HOME[session["role"]]))
    return render_template("index.html")


@app.route("/register/<role>", methods=["GET", "POST"])
def register(role):
    if role not in SERVICES:
        abort(404)
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        errors, account = SERVICES[role].register(request.form)
        if not errors:
            flash("Account created! You can log in now.", "ok")
            return redirect(url_for("login", role=role))
        for e in errors:
            flash(e, "error")
    return render_template("register.html", role=role, form=form, vehicles=VEHICLES)


@app.route("/login/<role>", methods=["GET", "POST"])
def login(role):
    if role not in ROLE_LABELS:
        abort(404)
    if request.method == "POST":
        password = request.form.get("password", "")
        if role == "admin":
            if admin_store.verify(password):
                session.clear()
                session.update(role="admin", uid=0, name="Administrator")
                return redirect(url_for("admin_dashboard"))
        else:
            acct = SERVICES[role].authenticate(request.form.get("name", ""), password)
            if acct:
                session.clear()
                session.update(role=role, uid=acct["id"], name=acct["full_name"])
                return redirect(url_for(HOME[role]))
        flash("Incorrect login details. Please try again.", "error")
    return render_template("login.html", role=role)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "ok")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Admin module
# --------------------------------------------------------------------------
@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    all_orders = orders_repo.all()
    by_status = {s: 0 for s in STATUS_LABELS}
    for o in all_orders:
        by_status[o["status"]] += 1
    brgy_counts = {}
    for o in all_orders:
        brgy_counts[o["barangay"]] = brgy_counts.get(o["barangay"], 0) + 1
    top_brgy = sorted(brgy_counts.items(), key=lambda kv: kv[1], reverse=True)[:6]
    stats = {
        "users": len(users_repo.all()),
        "riders": len(riders_repo.all()),
        "orders": len(all_orders),
        "completed": by_status["delivered"],
        "sales": sum(o["total"] for o in all_orders if o["status"] == "delivered"),
    }
    recent = sorted(all_orders, key=lambda o: o["id"], reverse=True)[:6]
    return render_template("admin/dashboard.html", stats=stats, by_status=by_status,
                           top_brgy=top_brgy, recent=recent)


@app.route("/admin/users")
@login_required("admin")
def admin_users():
    kind = request.args.get("kind", "user")
    if kind not in SERVICES:
        kind = "user"
    q = request.args.get("q", "").strip().lower()
    rows = SERVICES[kind].repo.all()
    if q:
        rows = [r for r in rows if q in r["full_name"].lower() or q in r["contact"]
                or q == str(r["id"])]
    return render_template("admin/users.html", kind=kind, q=q, rows=rows,
                           counts={"user": len(users_repo.all()),
                                   "rider": len(riders_repo.all())})


@app.route("/admin/users/<kind>/<int:uid>")
@login_required("admin")
def admin_user_detail(kind, uid):
    if kind not in SERVICES:
        abort(404)
    acct = SERVICES[kind].repo.get(uid)
    if not acct:
        abort(404)
    key = "user_id" if kind == "user" else "rider_id"
    history = [o for o in orders_repo.all() if o[key] == uid]
    history.sort(key=lambda o: o["id"], reverse=True)
    return render_template("admin/user_detail.html", kind=kind, acct=acct, history=history)


@app.route("/admin/orders")
@login_required("admin")
def admin_orders():
    status = request.args.get("status", "delivered")
    rows = orders_repo.all()
    if status in STATUS_LABELS:
        rows = [o for o in rows if o["status"] == status]
    else:
        status = "all"
    rows.sort(key=lambda o: o["id"], reverse=True)
    return render_template("admin/orders.html", rows=rows, status=status,
                           labels=STATUS_LABELS)


@app.route("/admin/barangays", methods=["GET", "POST"])
@login_required("admin")
def admin_barangays():
    if request.method == "POST":
        name = " ".join(request.form.get("name", "").split())
        try:
            fee = round(float(request.form.get("fee", "")), 2)
            if fee < 0:
                raise ValueError
        except ValueError:
            flash("Delivery fee must be a number (0 or more).", "error")
            return redirect(url_for("admin_barangays"))
        if not name:
            flash("Barangay name is required.", "error")
        elif any(b["name"].lower() == name.lower() for b in barangays_repo.all()):
            flash(f"{name} already exists. Edit its fee in the table below.", "error")
        else:
            barangays_repo.add({"name": name, "fee": fee})
            flash(f"Added {name} with a delivery fee of {peso(fee)}.", "ok")
        return redirect(url_for("admin_barangays"))
    return render_template("admin/barangays.html", rows=barangays_repo.all())


@app.route("/admin/barangays/<int:bid>/fee", methods=["POST"])
@login_required("admin")
def admin_barangay_fee(bid):
    try:
        fee = round(float(request.form.get("fee", "")), 2)
        if fee < 0:
            raise ValueError
    except ValueError:
        flash("Delivery fee must be a number (0 or more).", "error")
        return redirect(url_for("admin_barangays"))
    b = barangays_repo.update(bid, fee=fee)
    if not b:
        abort(404)
    flash(f"{b['name']} delivery fee is now {peso(fee)}. Existing orders keep their old fee.", "ok")
    return redirect(url_for("admin_barangays"))


# --------------------------------------------------------------------------
# Customer module
# --------------------------------------------------------------------------
@app.route("/user", methods=["GET", "POST"])
@login_required("user")
def user_order():
    customer = current_account()
    if request.method == "POST":
        errors, order = orders.create(customer, request.form)
        if not errors:
            flash("Order placed! A rider will pick it up soon.", "ok")
            return redirect(url_for("user_order_detail", oid=order["id"]))
        for e in errors:
            flash(e, "error")
    return render_template("user/order.html", barangays=barangays_repo.all(),
                           form=request.form if request.method == "POST" else {})


@app.route("/user/orders")
@login_required("user")
def user_orders():
    status = request.args.get("status", "all")
    mine = [o for o in orders_repo.all() if o["user_id"] == session["uid"]]
    if status == "successful":
        mine = [o for o in mine if o["status"] == "delivered"]
    elif status == "active":
        mine = [o for o in mine if o["status"] in ("pending", "accepted")]
    elif status == "cancelled":
        mine = [o for o in mine if o["status"] == "cancelled"]
    else:
        status = "all"
    mine.sort(key=lambda o: o["id"], reverse=True)
    return render_template("user/orders.html", rows=mine, status=status)


@app.route("/user/orders/<int:oid>")
@login_required("user")
def user_order_detail(oid):
    o = orders_repo.get(oid)
    if not o or o["user_id"] != session["uid"]:
        abort(404)
    return render_template("user/order_detail.html", o=o)


@app.route("/user/orders/<int:oid>/cancel", methods=["POST"])
@login_required("user")
def user_cancel(oid):
    if orders.cancel(oid, session["uid"]):
        flash("Order cancelled.", "ok")
    else:
        flash("Only orders still waiting for a rider can be cancelled.", "error")
    return redirect(url_for("user_orders"))


# --------------------------------------------------------------------------
# Rider module
# --------------------------------------------------------------------------
@app.route("/rider")
@login_required("rider")
def rider_dashboard():
    rider = current_account()
    all_orders = orders_repo.all()
    mine = [o for o in all_orders if o["rider_id"] == rider["id"]]
    active = next((o for o in mine if o["status"] == "accepted"), None)
    done = sorted((o for o in mine if o["status"] == "delivered"),
                  key=lambda o: o["id"], reverse=True)
    available = sorted((o for o in all_orders if o["status"] == "pending"),
                       key=lambda o: o["id"])
    stats = {
        "completed": len(done),
        "earned": sum(o["fee"] for o in done),
        "available": len(available),
    }
    return render_template("rider/dashboard.html", rider=rider, active=active,
                           done=done, available=available, stats=stats)


@app.route("/rider/orders/<int:oid>/accept", methods=["POST"])
@login_required("rider")
def rider_accept(oid):
    error = orders.accept(oid, current_account())
    flash(error or "Order accepted. Head to the restaurant!", "error" if error else "ok")
    return redirect(url_for("rider_dashboard"))


@app.route("/rider/orders/<int:oid>/deliver", methods=["POST"])
@login_required("rider")
def rider_deliver(oid):
    if orders.deliver(oid, current_account()):
        flash("Delivery completed. Nice work!", "ok")
    else:
        flash("That order can't be marked as delivered.", "error")
    return redirect(url_for("rider_dashboard"))


if __name__ == "__main__":
    print("\n DALI is running.  Admin default password: admin123 "
          "(set DALI_ADMIN_PASSWORD before first run to change it)\n")
    app.run(debug=True)