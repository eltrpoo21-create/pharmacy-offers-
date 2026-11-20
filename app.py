import os
import sqlite3
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, abort
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "store.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB per upload
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
                customer_id INTEGER NOT NULL,
                offer_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'جديد',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(offer_id) REFERENCES offers(id)
            )
        """)


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
        raise ValueError("صيغة الصورة غير مدعومة. استخدم JPG أو PNG أو WEBP.")
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
        app.logger.warning("Email not sent: SMTP credentials are not configured.")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"طلب عرض جديد رقم #{order_id} - صيدليتك للعروض"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.set_content(
        f"""وصلك طلب جديد من موقع صيدليتك للعروض

رقم الطلب: {order_id}
اسم العميل: {customer['name']}
رقم الهاتف: {customer['phone']}

العرض: {offer['title']}
السعر الحالي: {offer['price']:.2f} جنيه
الكمية: {quantity}
الإجمالي: {offer['price'] * quantity:.2f} جنيه

يرجى التواصل مع العميل لتأكيد التوفر والتفاصيل.
هذا الطلب ليس تأكيدًا للبيع أو حجزًا نهائيًا.
"""
    )

    image_path = UPLOAD_DIR / offer["image"] if offer["image"] else None
    if image_path and image_path.exists():
        import mimetypes
        ctype, _ = mimetypes.guess_type(str(image_path))
        maintype, subtype = (ctype or "image/jpeg").split("/", 1)
        with open(image_path, "rb") as img:
            msg.add_attachment(
                img.read(), maintype=maintype, subtype=subtype,
                filename=image_path.name
            )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception:
        app.logger.exception("Could not send order email")
        return False


@app.route("/")
def home():
    with db() as conn:
        offers = conn.execute(
            "SELECT * FROM offers WHERE active=1 ORDER BY id DESC"
        ).fetchall()
    return render_template("home.html", offers=offers)


@app.route("/login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(name) < 2 or len(digits) < 8 or len(digits) > 15:
            flash("اكتب اسمًا صحيحًا ورقم هاتف صحيحًا.", "error")
            return render_template("login.html")
        with db() as conn:
            conn.execute(
                """INSERT INTO customers(name, phone) VALUES(?, ?)
                   ON CONFLICT(phone) DO UPDATE SET name=excluded.name""",
                (name, digits)
            )
            customer = conn.execute(
                "SELECT * FROM customers WHERE phone=?", (digits,)
            ).fetchone()
        session["customer_id"] = customer["id"]
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
        flash("سجل اسمك ورقم هاتفك أولاً لإرسال الطلب.", "error")
        return redirect(url_for("customer_login"))

    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        quantity = 1
    quantity = max(1, min(quantity, 20))

    with db() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE id=? AND active=1", (offer_id,)
        ).fetchone()
        customer = conn.execute(
            "SELECT * FROM customers WHERE id=?", (customer_id,)
        ).fetchone()
        if not offer or not customer:
            abort(404)
        cur = conn.execute(
            "INSERT INTO orders(customer_id, offer_id, quantity) VALUES(?, ?, ?)",
            (customer_id, offer_id, quantity)
        )
        order_id = cur.lastrowid

    email_sent = send_order_email(order_id, customer, offer, quantity)
    return render_template("order_done.html", order_id=order_id, email_sent=email_sent)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = os.getenv("ADMIN_USERNAME", "admin")
        password_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
        password = request.form.get("password", "")
        submitted_user = request.form.get("username", "")
        if submitted_user == username and password_hash and check_password_hash(password_hash, password):
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("بيانات الدخول غير صحيحة. راجع إعدادات المدير في ملف .env.", "error")
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
        orders = conn.execute("""
            SELECT orders.*, customers.name AS customer_name,
                   customers.phone AS customer_phone, offers.title AS offer_title,
                   offers.price AS offer_price
            FROM orders
            JOIN customers ON customers.id=orders.customer_id
            JOIN offers ON offers.id=orders.offer_id
            ORDER BY orders.id DESC LIMIT 100
        """).fetchall()
        customers = conn.execute(
            "SELECT * FROM customers ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return render_template(
        "admin.html", offers=offers, orders=orders, customers=customers
    )


@app.route("/admin/offers/add", methods=["POST"])
@admin_required
def add_offer():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    try:
        price = float(request.form.get("price", ""))
        old_price_raw = request.form.get("old_price", "").strip()
        old_price = float(old_price_raw) if old_price_raw else None
        if price <= 0 or (old_price is not None and old_price <= 0):
            raise ValueError()
    except ValueError:
        flash("راجع الأسعار، يجب أن تكون أرقامًا أكبر من صفر.", "error")
        return redirect(url_for("admin_dashboard"))
    if not title:
        flash("اسم العرض مطلوب.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        image = save_image(request.files.get("image"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_dashboard"))
    with db() as conn:
        conn.execute(
            """INSERT INTO offers(title, description, old_price, price, image)
               VALUES(?, ?, ?, ?, ?)""",
            (title, description, old_price, price, image)
        )
    flash("تمت إضافة العرض.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/<int:offer_id>/edit", methods=["POST"])
@admin_required
def edit_offer(offer_id):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    try:
        price = float(request.form.get("price", ""))
        old_raw = request.form.get("old_price", "").strip()
        old_price = float(old_raw) if old_raw else None
        if not title or price <= 0 or (old_price is not None and old_price <= 0):
            raise ValueError()
    except ValueError:
        flash("راجع اسم العرض والأسعار.", "error")
        return redirect(url_for("admin_dashboard"))
    with db() as conn:
        current = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not current:
            abort(404)
        image = current["image"]
        try:
            new_image = save_image(request.files.get("image"))
            if new_image:
                image = new_image
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_dashboard"))
        conn.execute(
            """UPDATE offers SET title=?, description=?, old_price=?, price=?, image=?
               WHERE id=?""",
            (title, description, old_price, price, image, offer_id)
        )
    flash("تم تحديث العرض.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/<int:offer_id>/toggle", methods=["POST"])
@admin_required
def toggle_offer(offer_id):
    with db() as conn:
        conn.execute(
            "UPDATE offers SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
            (offer_id,)
        )
    flash("تم تغيير حالة العرض.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/<int:offer_id>/delete", methods=["POST"])
@admin_required
def delete_offer(offer_id):
    with db() as conn:
        conn.execute("DELETE FROM offers WHERE id=?", (offer_id,))
    flash("تم حذف العرض.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
@admin_required
def update_order_status(order_id):
    status = request.form.get("status", "جديد")
    allowed = {"جديد", "تم التواصل", "مكتمل", "ملغي"}
    if status not in allowed:
        abort(400)
    with db() as conn:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    flash("تم تحديث حالة الطلب.", "success")
    return redirect(url_for("admin_dashboard"))


@app.errorhandler(413)
def too_large(_error):
    return "حجم الصورة أكبر من 5 ميجابايت. ارجع واستخدم صورة أصغر.", 413


init_db()

if __name__ == "__main__":
    app.run(debug=True)
