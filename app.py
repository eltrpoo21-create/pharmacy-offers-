import os
import re
import time
import sqlite3
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, abort, send_from_directory
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.utils import secure_filename

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR))

DB_PATH = DATA_DIR / "store.db"
UPLOAD_DIR = DATA_DIR / "uploads"
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}

# يفضل تغيير هذه القيم من خلال Environment Variables في Railway
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Mohamed2005")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "pharmacy-offers-secret-key-2026-secure")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

csrf = CSRFProtect(app)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# سجل لحماية صفحة الأدمن من محاولات التخمين المتكررة
LOGIN_ATTEMPTS = {}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL DEFAULT '',
                is_verified INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

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
                customer_id INTEGER,
                customer_name TEXT NOT NULL DEFAULT '',
                customer_phone TEXT NOT NULL DEFAULT '',
                customer_address TEXT NOT NULL DEFAULT '',
                items_summary TEXT NOT NULL DEFAULT '',
                total_price REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'جديد',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE SET NULL
            )
        """)

        # ترقية الجداول القديمة تلقائياً بدون مسح البيانات
        order_cols = [row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "customer_name" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''")
        if "customer_phone" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN customer_phone TEXT NOT NULL DEFAULT ''")
        if "customer_address" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN customer_address TEXT NOT NULL DEFAULT ''")
        if "items_summary" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN items_summary TEXT NOT NULL DEFAULT ''")
        if "total_price" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN total_price REAL NOT NULL DEFAULT 0")


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
        raise ValueError("صيغة الصورة غير مدعومة. المسموح: jpg, png, webp.")
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = secure_filename(f"{secrets.token_hex(12)}.{ext}")
    file.save(UPLOAD_DIR / filename)
    return filename


def get_cart():
    cart = session.get("cart", {})
    return cart if isinstance(cart, dict) else {}


def save_cart(cart):
    session["cart"] = cart
    session.modified = True


@app.context_processor
def inject_globals():
    cart = get_cart()
    cart_count = sum(int(qty) for qty in cart.values())
    return {"cart_count": cart_count}


def send_order_email(order_id, customer_name, customer_phone, customer_address, items_summary, total_price):
    recipient = os.getenv("ORDER_EMAIL", "eltrpoo21@gmail.com")
    smtp_host = os.getenv("SMTP_HOST", "://gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    if not smtp_user or not smtp_password:
        return False
    msg = EmailMessage()
    msg["Subject"] = f"طلب جديد رقم #{order_id} - عروض الصيدلية"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.set_content(
        f"رقم الطلب: #{order_id}\n"
        f"العميل: {customer_name}\n"
        f"الهاتف: {customer_phone}\n"
        f"العنوان: {customer_address}\n"
        f"تفاصيل الطلب:\n{items_summary}\n"
        f"الإجمالي: {total_price:.2f} جنيه"
    )
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception:
        return False


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


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
                conn.execute(
                    "UPDATE customers SET name=?, address=?, is_verified=1 WHERE id=?",
                    (name, address, existing["id"])
                )
                session["customer_id"] = existing["id"]
                flash("تم تسجيل دخولك وتحديث بياناتك بنجاح!", "success")
            else:
                cur = conn.execute(
                    "INSERT INTO customers(name, phone, address, is_verified) VALUES(?, ?, ?, 1)",
                    (name, digits, address)
                )
                session["customer_id"] = cur.lastrowid
                flash("تم تسجيل حسابك بنجاح!", "success")

        next_page = request.args.get("next")
        if next_page == "cart":
            return redirect(url_for("view_cart"))
        return redirect(url_for("home"))

    return render_template("login.html")


@app.route("/logout")
def customer_logout():
    session.pop("customer_id", None)
    return redirect(url_for("home"))


# ==================== سلة المشتريات ====================
@app.route("/cart/add/<int:offer_id>", methods=["POST"])
def add_to_cart(offer_id):
    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        quantity = 1
    quantity = max(1, min(quantity, 20))

    with db() as conn:
        offer = conn.execute("SELECT * FROM offers WHERE id=? AND active=1", (offer_id,)).fetchone()
        if not offer:
            flash("هذا العرض غير متاح حالياً.", "error")
            return redirect(url_for("home"))

    cart = get_cart()
    key = str(offer_id)
    cart[key] = min(20, int(cart.get(key, 0)) + quantity)
    save_cart(cart)
    flash(f"تمت إضافة ({offer['title']}) إلى السلة.", "success")
    return redirect(url_for("home"))


@app.route("/cart")
def view_cart():
    cart = get_cart()
    items = []
    total = 0.0
    if cart:
        with db() as conn:
            for offer_id_str, qty in list(cart.items()):
                offer = conn.execute("SELECT * FROM offers WHERE id=? AND active=1", (int(offer_id_str),)).fetchone()
                if offer:
                    subtotal = float(offer["price"]) * int(qty)
                    total += subtotal
                    items.append({
                        "offer": offer,
                        "quantity": int(qty),
                        "subtotal": subtotal
                    })
                else:
                    cart.pop(offer_id_str, None)
            save_cart(cart)
    return render_template("cart.html", items=items, total=total)


@app.route("/cart/update/<int:offer_id>", methods=["POST"])
def update_cart(offer_id):
    cart = get_cart()
    key = str(offer_id)
    try:
        qty = int(request.form.get("quantity", "1"))
    except ValueError:
        qty = 1

    if qty <= 0:
        cart.pop(key, None)
    else:
        cart[key] = min(20, qty)
    save_cart(cart)
    return redirect(url_for("view_cart"))


@app.route("/cart/remove/<int:offer_id>", methods=["POST"])
def remove_from_cart(offer_id):
    cart = get_cart()
    cart.pop(str(offer_id), None)
    save_cart(cart)
    flash("تم حذف الصنف من السلة.", "success")
    return redirect(url_for("view_cart"))


@app.route("/cart/checkout", methods=["POST"])
def checkout_cart():
    customer_id = session.get("customer_id")
    if not customer_id:
        flash("سجّل اسمك ورقم هاتفك وعنوانك أولاً لإتمام الطلب.", "error")
        return redirect(url_for("customer_login", next="cart"))

    cart = get_cart()
    if not cart:
        flash("سلة المشتريات فارغة!", "error")
        return redirect(url_for("home"))

    with db() as conn:
        customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            session.pop("customer_id", None)
            flash("يرجى تسجيل بياناتك مرة أخرى.", "error")
            return redirect(url_for("customer_login", next="cart"))

        summary_lines = []
        total_price = 0.0
        for offer_id_str, qty in cart.items():
            offer = conn.execute("SELECT * FROM offers WHERE id=? AND active=1", (int(offer_id_str),)).fetchone()
            if offer:
                qty = int(qty)
                line_total = float(offer["price"]) * qty
                total_price += line_total
                summary_lines.append(f"• {offer['title']} (الكمية: {qty}) = {line_total:.2f} ج")

        if not summary_lines:
            flash("العروض المختارة لم تعد متاحة.", "error")
            save_cart({})
            return redirect(url_for("home"))

        items_summary = "\n".join(summary_lines)
        cur = conn.execute(
            """
            INSERT INTO orders(
                customer_id, customer_name, customer_phone,
                customer_address, items_summary, total_price
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                customer["id"],
                customer["name"],
                customer["phone"],
                customer["address"],
                items_summary,
                total_price
            )
        )
        order_id = cur.lastrowid

    save_cart({})
    send_order_email(
        order_id,
        customer["name"],
        customer["phone"],
        customer["address"],
        items_summary,
        total_price
    )
    flash(f"تم إرسال طلبك رقم #{order_id} بنجاح! سنتواصل معك قريباً.", "success")
    return redirect(url_for("home"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    ip = request.remote_addr or "unknown"
    now = time.time()
    attempts, lock_until = LOGIN_ATTEMPTS.get(ip, (0, 0))

    if now < lock_until:
        remaining = int((lock_until - now) // 60) + 1
        flash(f"تم حظر المحاولات مؤقتاً. حاول بعد {remaining} دقيقة.", "error")
        return render_template("admin_login.html")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
            LOGIN_ATTEMPTS.pop(ip, None)
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))

        attempts += 1
        if attempts >= 5:
            LOGIN_ATTEMPTS[ip] = (0, now + 300)
            flash("تم تجاوز الحد المسموح لمحاولات الدخول. حاول بعد 5 دقائق.", "error")
        else:
            LOGIN_ATTEMPTS[ip] = (attempts, 0)
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
        orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    return render_template("admin.html", offers=offers, customers=customers, orders=orders)


@app.route("/admin/customers/delete/<int:customer_id>", methods=["POST"])
@admin_required
def delete_customer(customer_id):
    mode = request.form.get("mode", "keep_orders")
    with db() as conn:
        if mode == "with_orders":
            conn.execute("DELETE FROM orders WHERE customer_id=?", (customer_id,))
            conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
            flash("تم حذف العميل وجميع طلباته.", "success")
        else:
            conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
            flash("تم حذف حساب العميل مع الإبقاء على فواتيره في سجل الطلبات.", "success")
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


@app.route("/admin/orders/delete/<int:order_id>", methods=["POST"])
@admin_required
def delete_order(order_id):
    with db() as conn:
        conn.execute("DELETE FROM orders WHERE id=?", (order_id,))
    flash("تم حذف الطلب.", "success")
    return redirect(url_for("admin_dashboard"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
