import os
import re
import sqlite3
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import quote

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

# رقم الواتساب الذي ستصلك عليه رسائل تفعيل العملاء (بصيغة دولية بدون +)
ADMIN_WHATSAPP = "201001497324"

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
                otp_code TEXT DEFAULT '',
                is_verified INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # تحديث تلقائي لقاعدة البيانات القديمة إذا كانت موجودة بدون الأعمدة الجديدة
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(customers)").fetchall()]
        if "otp_code" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN otp_code TEXT DEFAULT ''")
        if "is_verified" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0")

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
    return "." in filename and filename.rsplit(".", 1).lower() in ALLOWED_EXTENSIONS


def save_image(file):
    if not file or not file.filename:
        return ""
    if not allowed_file(file.filename):
        raise ValueError("صيغة الصورة غير مدعومة. استخدم JPG أو PNG أو WEBP.")
    ext = file.filename.rsplit(".", 1).lower()
    filename = secure_filename(f"{secrets.token_hex(12)}.{ext}")
    file.save(UPLOAD_DIR / filename)
    return filename


def send_order_email(order_id, customer, offer, quantity):
    recipient = os.getenv("ORDER_EMAIL", "eltrpoo21@gmail.com")
    smtp_host = os.getenv("SMTP_HOST", "://gmail.com")
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
        raw_phone = request.form.get("phone", "").strip()
        digits = normalize_egypt_phone(raw_phone)

        # التحقق من الاسم ورقم الهاتف المصري الصحيح
        if len(name) < 2 or not re.match(r"^01[0-9]{8}$", digits):
            flash("اكتب اسمًا صحيحًا ورقم موبايل مصري صحيح مكون من 11 رقماً.", "error")
            return render_template("login.html")

        with db() as conn:
            existing = conn.execute(
                "SELECT * FROM customers WHERE phone=?", (digits,)
            ).fetchone()

            if existing:
                # منع تكرار التسجيل بنفس الرقم
                if existing["is_verified"] == 1:
                    flash("عذراً، هذا الرقم مسجل ومفعّل لدينا بالفعل ولا يمكن التسجيل به مرة أخرى.", "error")
                    return render_template("login.html")
                else:
                    # إذا سجل من قبل ولم يكمل التفعيل، نعيد توجيهه لصفحة التفعيل
                    session["pending_customer_id"] = existing["id"]
                    return redirect(url_for("verify_whatsapp"))

            # توليد كود تحقق من 4 أرقام للعميل الجديد
            otp_code = str(secrets.randbelow(9000) + 1000)
            cur = conn.execute(
                "INSERT INTO customers(name, phone, otp_code, is_verified) VALUES(?, ?, ?, 0)",
                (name, digits, otp_code)
            )
            session["pending_customer_id"] = cur.lastrowid

        return redirect(url_for("verify_whatsapp"))
    return render_template("login.html")


@app.route("/verify-whatsapp")
def verify_whatsapp():
    pending_id = session.get("pending_customer_id")
    if not pending_id:
        return redirect(url_for("customer_login"))

    with db() as conn:
        customer = conn.execute(
            "SELECT * FROM customers WHERE id=?", (pending_id,)
        ).fetchone()

    if not customer:
        session.pop("pending_customer_id", None)
        return redirect(url_for("customer_login"))

    # إذا قام الأدمن بتفعيل الحساب، يتم تسجيل دخوله تلقائياً عند تحديث الصفحة
    if customer["is_verified"] == 1:
        session.pop("pending_customer_id", None)
        session["customer_id"] = customer["id"]
        flash("تم تفعيل حسابك بنجاح! يمكنك الآن طلب العروض.", "success")
        return redirect(url_for("home"))

    wa_text = (
        f"مرحباً، أرغب في تفعيل حسابي في صيدليتك للعروض\n"
        f"الاسم: {customer['name']}\n"
        f"رقم الهاتف: {customer['phone']}\n"
        f"كود التفعيل: {customer['otp_code']}"
    )
    wa_url = f"https://wa.me{ADMIN_WHATSAPP}?text={quote(wa_text)}"

    return render_template("verify.html", customer=customer, wa_url=wa_url)


@app.route("/logout")
def customer_logout():
    session.pop("customer_id", None)
    session.pop("pending_customer_id", None)
    return redirect(url_for("home"))


@app.route("/order/<int:offer_id>", methods=["POST"])
def place_order(offer_id):
    customer_id = session.get("customer_id")
    if not customer_id:
        if session.get("pending_customer_id"):
            flash("يجب تأكيد رقم هاتفك عبر واتساب وتفعيل الحساب أولاً قبل إرسال أي طلب.", "error")
            return redirect(url_for("verify_whatsapp"))
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

        # حماية إضافية: منع الطلب إذا تم إلغاء تفعيل العميل
        if customer["is_verified"] != 1:
            session.pop("customer_id", None)
app.config["PROPAGATE_EXCEPTIONS"] = True
init_db()

if __name__ == "__main__":
    app.run(debug=True)
