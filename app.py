import os
import re
import sqlite3
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from flask_wtf.csrf import CSRFProtect
from werkzeug.utils import secure_filename

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "store.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Mohamed2005"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
csrf = CSRFProtect(app)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL DEFAULT '',
                otp_code TEXT DEFAULT '',
                is_verified INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(customers)").fetchall()]
        if "address" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN address TEXT NOT NULL DEFAULT ''")
        if "otp_code" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN otp_code TEXT DEFAULT ''")
        if "is_verified" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE customers SET is_verified = 1")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                old_price REAL,
                price REAL NOT NULL,
                image TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                offer_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'جديد',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(offer_id) REFERENCES offers(id)
            )
        """)


def normalize_egypt_phone(raw_phone):
    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    if digits.startswith("20") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_image(file):
    if not file or not file.filename:
        return ""
    if not allowed_file(file.filename):
        raise ValueError("صيغة الصورة غير مدعومة.")
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = secure_filename(f"{secrets.token_hex(12)}.{ext}")
    file.save(UPLOAD_DIR / filename)
    return filename


def send_order_email(order_id, customer, offer, quantity):
    recipient = os.getenv("ORDER_EMAIL", "eltrpoo21@gmail.com")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    if not smtp_user or not smtp_password:
        return False
    msg = EmailMessage()
    msg["Subject"] = f"طلب عرض جديد رقم #{order_id} - صيدليتك للعروض"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.set_content(
        f"رقم الطلب: {order_id}\n"
        f"العميل: {customer['name']}\n"
        f"الهاتف: {customer['phone']}\n"
        f"العنوان: {customer['address']}\n"
        f"العرض: {offer['title']}\n"
        f"الكمية: {quantity}\n"
        f"الإجمالي: {offer['price'] * quantity:.2f} جنيه"
    )
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception:
        return False


@app.route("/")
def home():
    with db() as conn:
        offers = conn.execute("SELECT * FROM offers WHERE active=1 ORDER BY id DESC").fetchall()
    return render_template("home.html", offers=offers)


@app.route("/login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        raw_phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        digits = normalize_egypt_phone(raw_phone)

        if len(name) < 2 or not re.match(r"^01[0-9]{9}$", digits) or len(address) < 4:
            flash("اكتب اسماً صحيحاً، ورقم موبايل مصري صحيح (11 رقماً)، وعنواناً واضحاً.", "error")
            return render_template("login.html")

        with db() as conn:
            existing = conn.execute("SELECT * FROM customers WHERE phone=?", (digits,)).fetchone()
            if existing:
                conn.execute("UPDATE customers SET name=?, address=?, is_verified=1 WHERE id=?", (name, address, existing["id"]))
                session["customer_id"] = existing["id"]
                flash("تم تسجيل دخولك وتحديث بياناتك بنجاح!", "success")
                return redirect(url_for("home"))

            cur = conn.execute(
                "INSERT INTO customers(name, phone, address, is_verified) VALUES(?, ?, ?, 1)",
                (name, digits, address)
            )
            session["customer_id"] = cur.lastrowid
            flash("تم تسجيل حسابك بنجاح!", "success")
            return redirect(url_for("home"))

    return render_template("login.html")


@app.route("/logout")
def customer_logout():
    session.pop("customer_id", None)
    return redirect(url_for("home"))


@app.route("/order/<int:offer_id>", methods=["POST"])
def place_order(offer_id):
    customer_id = session.get("customer_id")
    if not customer_id:
        flash("سجل اسمك ورقم هاتفك وعنوانك أولاً لإرسال الطلب.", "error")
        return redirect(url_for("customer_login"))
    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        quantity = 1
    quantity = max(1, min(quantity, 20))
    with db() as conn:
        offer = conn.execute("SELECT * FROM offers WHERE id=? AND active=1", (offer_id,)).fetchone()
        customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not offer or not customer:
            abort(404)
        cur = conn.execute("INSERT INTO orders(customer_id, offer_id, quantity) VALUES(?, ?, ?)", (customer_id, offer_id, quantity))
        order_id = cur.lastrowid
    send_order_email(order_id, customer, offer, quantity)
    flash("تم إرسال طلبك بنجاح! سنتواصل معك قريباً.", "success")
    return redirect(url_for("home"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("بيانات الدخول غير صحيحة.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    with db() as conn:
        offers = conn.execute("SELECT * FROM offers ORDER BY id DESC").fetchall()
        customers = conn.execute("SELECT * FROM customers ORDER BY id DESC").fetchall()
        orders = conn.execute("""
            SELECT orders.*,
                   customers.name AS customer_name,
                   customers.phone AS customer_phone,
                   customers.address AS customer_address,
                   offers.title AS offer_title
            FROM orders
            JOIN customers ON orders.customer_id = customers.id
            JOIN offers ON orders.offer_id = offers.id
            ORDER BY orders.id DESC
        """).fetchall()
    return render_template("admin.html", offers=offers, customers=customers, orders=orders)


@app.route("/admin/customers/delete/<int:customer_id>", methods=["POST"])
@admin_required
def delete_customer(customer_id):
    with db() as conn:
        conn.execute("DELETE FROM orders WHERE customer_id=?", (customer_id,))
        conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    flash("تم حذف العميل وطلباته.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/add", methods=["POST"])
@admin_required
def add_offer():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    try:
        price = float(request.form.get("price", "0"))
    except ValueError:
        price = 0
    old_price_raw = request.form.get("old_price", "").strip()
    try:
        old_price = float(old_price_raw) if old_price_raw else None
    except ValueError:
        old_price = None

    image_filename = ""
    if "image" in request.files and request.files["image"].filename:
        try:
            image_filename = save_image(request.files["image"])
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("admin_dashboard"))

    if title and price > 0:
        with db() as conn:
            conn.execute(
                "INSERT INTO offers(title, description, old_price, price, image) VALUES(?, ?, ?, ?, ?)",
                (title, description, old_price, price, image_filename)
            )
        flash("تمت إضافة العرض بنجاح.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/edit/<int:offer_id>", methods=["POST"])
@admin_required
def edit_offer(offer_id):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    try:
        price = float(request.form.get("price", "0"))
    except ValueError:
        price = 0
    old_price_raw = request.form.get("old_price", "").strip()
    try:
        old_price = float(old_price_raw) if old_price_raw else None
    except ValueError:
        old_price = None

    with db() as conn:
        offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not offer:
            abort(404)
        image_filename = offer["image"]
        if "image" in request.files and request.files["image"].filename:
            try:
                image_filename = save_image(request.files["image"])
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("admin_dashboard"))

        if title and price > 0:
            conn.execute(
                "UPDATE offers SET title=?, description=?, old_price=?, price=?, image=? WHERE id=?",
                (title, description, old_price, price, image_filename, offer_id)
            )
            flash("تم تحديث العرض بنجاح.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/toggle/<int:offer_id>", methods=["POST"])
@admin_required
def toggle_offer(offer_id):
    with db() as conn:
        offer = conn.execute("SELECT active FROM offers WHERE id=?", (offer_id,)).fetchone()
        if offer:
            new_state = 0 if offer["active"] else 1
            conn.execute("UPDATE offers SET active=? WHERE id=?", (new_state, offer_id))
            flash("تم تحديث حالة العرض.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/delete/<int:offer_id>", methods=["POST"])
@admin_required
def delete_offer(offer_id):
    with db() as conn:
        conn.execute("DELETE FROM orders WHERE offer_id=?", (offer_id,))
        conn.execute("DELETE FROM offers WHERE id=?", (offer_id,))
    flash("تم حذف العرض.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/status/<int:order_id>", methods=["POST"])
@admin_required
def update_order_status(order_id):
    status = request.form.get("status", "جديد").strip()
    with db() as conn:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    flash("تم تحديث حالة الطلب.", "success")
    return redirect(url_for("admin_dashboard"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
