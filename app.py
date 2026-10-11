"""
SpendWise - Smart Expense Tracker
Flask backend: auth, CRUD, auto-categorization, CSV import,
budget alerts, chart/insight APIs and Excel/CSV export.
"""

import io
import os
from datetime import date, datetime, timedelta

import pandas as pd
from dotenv import load_dotenv
from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import extract, func
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///expenses.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB upload limit

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ----------------------------------------------------------------------------
# Auto-categorization rules (keyword -> category). Extend freely.
# ----------------------------------------------------------------------------
CATEGORY_RULES = {
    "Food": ["swiggy", "zomato", "restaurant", "cafe", "dominos", "pizza",
             "kfc", "mcdonald", "biryani", "hotel", "bakery", "tea", "juice"],
    "Groceries": ["bigbasket", "blinkit", "zepto", "dmart", "grocery",
                  "supermarket", "reliance fresh", "instamart"],
    "Travel": ["uber", "ola", "rapido", "irctc", "redbus", "metro", "petrol",
               "fuel", "bus", "flight", "indigo", "railway"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "meesho", "mall"],
    "Bills": ["electricity", "tneb", "recharge", "airtel", "jio", "vi ",
              "broadband", "wifi", "gas", "water bill", "dth"],
    "Entertainment": ["netflix", "spotify", "hotstar", "prime video",
                      "bookmyshow", "movie", "youtube", "game"],
    "Education": ["udemy", "coursera", "guvi", "college", "fees", "book",
                  "exam", "tuition"],
    "Health": ["pharmacy", "apollo", "hospital", "medical", "clinic", "medplus"],
}
CATEGORIES = list(CATEGORY_RULES.keys()) + ["Salary", "Other"]


def auto_categorize(description: str) -> str:
    """Return a category based on keywords found in the description."""
    text = (description or "").lower()
    for category, keywords in CATEGORY_RULES.items():
        if any(k in text for k in keywords):
            return category
    return "Other"


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    monthly_budget = db.Column(db.Float, default=0.0)
    transactions = db.relationship("Transaction", backref="user", lazy=True,
                                   cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    type = db.Column(db.String(10), nullable=False)  # "income" or "expense"
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False, default="Other")
    description = db.Column(db.String(200), default="")
    date = db.Column(db.Date, nullable=False, default=date.today, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "amount": self.amount,
            "category": self.category,
            "description": self.description,
            "date": self.date.isoformat(),
        }


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def parse_month(month_str):
    """'2026-10' -> (start_date, end_date_exclusive). Defaults to current month."""
    try:
        start = datetime.strptime(month_str, "%Y-%m").date()
    except (TypeError, ValueError):
        start = date.today().replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def user_transactions(start=None, end=None):
    q = Transaction.query.filter_by(user_id=current_user.id)
    if start and end:
        q = q.filter(Transaction.date >= start, Transaction.date < end)
    return q


def month_totals(start, end):
    rows = (db.session.query(Transaction.type, func.coalesce(func.sum(Transaction.amount), 0))
            .filter(Transaction.user_id == current_user.id,
                    Transaction.date >= start, Transaction.date < end)
            .group_by(Transaction.type).all())
    totals = {"income": 0.0, "expense": 0.0}
    for t, total in rows:
        totals[t] = float(total)
    return totals


def budget_status(spent):
    """Return (percent_used, level) where level is ok / warning / exceeded / none."""
    budget = current_user.monthly_budget or 0
    if budget <= 0:
        return 0, "none"
    pct = round(spent / budget * 100, 1)
    if pct >= 100:
        return pct, "exceeded"
    if pct >= 80:
        return pct, "warning"
    return pct, "ok"


def parse_statement(file_stream):
    """
    Read a bank/UPI CSV into a list of dicts: date, description, amount, type.
    Supports either separate Debit/Credit columns, or a single Amount column
    (negative = expense, positive = income) with an optional Type/Dr-Cr column.
    """
    df = pd.read_csv(file_stream)
    df.columns = [str(c).strip().lower() for c in df.columns]

    def find(options):
        for c in df.columns:
            if any(o in c for o in options):
                return c
        return None

    date_col = find(["date"])
    desc_col = find(["description", "narration", "particulars", "details", "remarks"])
    debit_col = find(["debit", "withdrawal"])
    credit_col = find(["credit", "deposit"])
    amount_col = None if (debit_col or credit_col) else find(["amount"])
    type_col = find(["type", "dr/cr", "drcr"])

    if not date_col or not (debit_col or credit_col or amount_col):
        raise ValueError("CSV needs a date column and an amount (or debit/credit) column.")

    def to_num(series):
        return pd.to_numeric(series.astype(str).str.replace(",", "").str.strip(),
                             errors="coerce").fillna(0)

    df["_date"] = pd.to_datetime(df[date_col], dayfirst=True, errors="coerce")
    records = []

    for i, row in df.iterrows():
        if pd.isna(row["_date"]):
            continue
        desc = str(row[desc_col]).strip() if desc_col and pd.notna(row[desc_col]) else ""

        if debit_col or credit_col:
            debit = to_num(df[debit_col]).iloc[df.index.get_loc(i)] if debit_col else 0
            credit = to_num(df[credit_col]).iloc[df.index.get_loc(i)] if credit_col else 0
            if debit > 0:
                amount, ttype = debit, "expense"
            elif credit > 0:
                amount, ttype = credit, "income"
            else:
                continue
        else:
            raw = to_num(df[amount_col]).iloc[df.index.get_loc(i)]
            if raw == 0:
                continue
            amount = abs(raw)
            ttype = "expense" if raw < 0 else "income"
            if type_col and pd.notna(row[type_col]):
                flag = str(row[type_col]).strip().lower()
                if flag in ("dr", "debit", "expense"):
                    ttype = "expense"
                elif flag in ("cr", "credit", "income"):
                    ttype = "income"

        records.append({"date": row["_date"].date(), "description": desc[:200],
                        "amount": float(amount), "type": ttype})
    return records


# ----------------------------------------------------------------------------
# Auth routes
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or len(password) < 6:
            flash("Fill all fields. Password must be at least 6 characters.", "danger")
        elif User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email already exists.", "danger")
        else:
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        identifier = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier.lower())).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


# ----------------------------------------------------------------------------
# Dashboard & transactions
# ----------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    start, end = parse_month(month)
    totals = month_totals(start, end)
    pct, level = budget_status(totals["expense"])
    recent = (user_transactions(start, end)
              .order_by(Transaction.date.desc(), Transaction.id.desc()).limit(8).all())
    return render_template(
        "dashboard.html", month=start.strftime("%Y-%m"), totals=totals,
        balance=totals["income"] - totals["expense"], budget=current_user.monthly_budget,
        budget_pct=pct, budget_level=level, recent=recent)


@app.route("/transactions")
@login_required
def transactions():
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    category = request.args.get("category", "")
    start, end = parse_month(month)
    q = user_transactions(start, end)
    if category:
        q = q.filter(Transaction.category == category)
    items = q.order_by(Transaction.date.desc(), Transaction.id.desc()).all()
    return render_template("transactions.html", items=items, month=start.strftime("%Y-%m"),
                           categories=CATEGORIES, selected_category=category)


@app.route("/transactions/add", methods=["GET", "POST"])
@login_required
def add_transaction():
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0))
            if amount <= 0:
                raise ValueError
            t_date = datetime.strptime(request.form.get("date"), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            flash("Enter a valid positive amount and date.", "danger")
            return render_template("transaction_form.html", categories=CATEGORIES,
                                   txn=None, today=date.today().isoformat())

        description = request.form.get("description", "").strip()[:200]
        t_type = request.form.get("type", "expense")
        category = request.form.get("category") or auto_categorize(description)
        if t_type not in ("income", "expense"):
            t_type = "expense"

        db.session.add(Transaction(user_id=current_user.id, type=t_type, amount=amount,
                                   category=category, description=description, date=t_date))
        db.session.commit()

        # Budget alert right after adding an expense
        if t_type == "expense":
            start, end = parse_month(t_date.strftime("%Y-%m"))
            pct, level = budget_status(month_totals(start, end)["expense"])
            if level == "exceeded":
                flash(f"Budget exceeded! You have used {pct}% of your monthly budget.", "danger")
            elif level == "warning":
                flash(f"Heads up: you have used {pct}% of your monthly budget.", "warning")

        flash("Transaction added.", "success")
        return redirect(url_for("transactions"))
    return render_template("transaction_form.html", categories=CATEGORIES,
                           txn=None, today=date.today().isoformat())


@app.route("/transactions/edit/<int:txn_id>", methods=["GET", "POST"])
@login_required
def edit_transaction(txn_id):
    txn = Transaction.query.filter_by(id=txn_id, user_id=current_user.id).first_or_404()
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0))
            if amount <= 0:
                raise ValueError
            txn.date = datetime.strptime(request.form.get("date"), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            flash("Enter a valid positive amount and date.", "danger")
            return render_template("transaction_form.html", categories=CATEGORIES,
                                   txn=txn, today=date.today().isoformat())
        txn.amount = amount
        txn.description = request.form.get("description", "").strip()[:200]
        txn.type = request.form.get("type", txn.type)
        txn.category = request.form.get("category") or auto_categorize(txn.description)
        db.session.commit()
        flash("Transaction updated.", "success")
        return redirect(url_for("transactions"))
    return render_template("transaction_form.html", categories=CATEGORIES,
                           txn=txn, today=date.today().isoformat())


@app.route("/transactions/delete/<int:txn_id>", methods=["POST"])
@login_required
def delete_transaction(txn_id):
    txn = Transaction.query.filter_by(id=txn_id, user_id=current_user.id).first_or_404()
    db.session.delete(txn)
    db.session.commit()
    flash("Transaction deleted.", "info")
    return redirect(url_for("transactions"))


# ----------------------------------------------------------------------------
# Budget & CSV import
# ----------------------------------------------------------------------------
@app.route("/budget", methods=["POST"])
@login_required
def set_budget():
    try:
        value = float(request.form.get("budget", 0))
        if value < 0:
            raise ValueError
    except ValueError:
        flash("Enter a valid budget amount.", "danger")
        return redirect(url_for("dashboard"))
    current_user.monthly_budget = value
    db.session.commit()
    flash("Monthly budget updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/import", methods=["GET", "POST"])
@login_required
def import_statement():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename.lower().endswith(".csv"):
            flash("Please upload a .csv file.", "danger")
            return redirect(url_for("import_statement"))
        try:
            records = parse_statement(file)
        except Exception as exc:  # bad format, empty file, etc.
            flash(f"Could not read the file: {exc}", "danger")
            return redirect(url_for("import_statement"))

        added = skipped = 0
        for r in records:
            duplicate = Transaction.query.filter_by(
                user_id=current_user.id, date=r["date"], amount=r["amount"],
                description=r["description"], type=r["type"]).first()
            if duplicate:
                skipped += 1
                continue
            category = "Salary" if (r["type"] == "income" and "salary" in r["description"].lower()) \
                else auto_categorize(r["description"])
            db.session.add(Transaction(user_id=current_user.id, category=category, **r))
            added += 1
        db.session.commit()
        flash(f"Imported {added} transactions ({skipped} duplicates skipped).", "success")
        return redirect(url_for("transactions"))
    return render_template("import.html")


# ----------------------------------------------------------------------------
# JSON APIs for charts and insights
# ----------------------------------------------------------------------------
@app.route("/api/chart-data")
@login_required
def chart_data():
    start, end = parse_month(request.args.get("month"))

    by_category = (db.session.query(Transaction.category, func.sum(Transaction.amount))
                   .filter(Transaction.user_id == current_user.id, Transaction.type == "expense",
                           Transaction.date >= start, Transaction.date < end)
                   .group_by(Transaction.category).all())

    daily = (db.session.query(Transaction.date, func.sum(Transaction.amount))
             .filter(Transaction.user_id == current_user.id, Transaction.type == "expense",
                     Transaction.date >= start, Transaction.date < end)
             .group_by(Transaction.date).order_by(Transaction.date).all())

    # Last 6 months income vs expense
    six_months_ago = (start.replace(day=1) - timedelta(days=150)).replace(day=1)
    monthly = (db.session.query(extract("year", Transaction.date), extract("month", Transaction.date),
                                Transaction.type, func.sum(Transaction.amount))
               .filter(Transaction.user_id == current_user.id, Transaction.date >= six_months_ago)
               .group_by(extract("year", Transaction.date), extract("month", Transaction.date),
                         Transaction.type).all())
    trend = {}
    for y, m, t, total in monthly:
        key = f"{int(y)}-{int(m):02d}"
        trend.setdefault(key, {"income": 0, "expense": 0})[t] = float(total)
    trend_labels = sorted(trend)

    return jsonify({
        "category": {"labels": [c for c, _ in by_category],
                     "values": [round(float(v), 2) for _, v in by_category]},
        "daily": {"labels": [d.isoformat() for d, _ in daily],
                  "values": [round(float(v), 2) for _, v in daily]},
        "trend": {"labels": trend_labels,
                  "income": [trend[k]["income"] for k in trend_labels],
                  "expense": [trend[k]["expense"] for k in trend_labels]},
    })


@app.route("/api/insights")
@login_required
def insights():
    start, end = parse_month(request.args.get("month"))
    items = user_transactions(start, end).filter_by(type="expense").all()
    messages = []

    if not items:
        return jsonify({"insights": ["Add some expenses to see insights."]})

    total = sum(t.amount for t in items)

    # Top category
    cat_totals = {}
    for t in items:
        cat_totals[t.category] = cat_totals.get(t.category, 0) + t.amount
    top_cat, top_val = max(cat_totals.items(), key=lambda kv: kv[1])
    messages.append(f"Your biggest spending category is {top_cat} "
                    f"(Rs. {top_val:,.0f}, {top_val / total * 100:.0f}% of expenses).")

    # Weekend vs weekday average daily spend
    days_in_month = (end - start).days
    weekend_days = sum(1 for i in range(days_in_month) if (start + timedelta(days=i)).weekday() >= 5)
    weekday_days = days_in_month - weekend_days
    weekend_total = sum(t.amount for t in items if t.date.weekday() >= 5)
    weekday_total = total - weekend_total
    if weekend_days and weekday_days and weekday_total > 0:
        we_avg, wd_avg = weekend_total / weekend_days, weekday_total / weekday_days
        diff = (we_avg - wd_avg) / wd_avg * 100
        if abs(diff) >= 10:
            word = "more" if diff > 0 else "less"
            messages.append(f"You spend {abs(diff):.0f}% {word} per day on weekends than on weekdays.")

    # Compare with previous month
    prev_start, prev_end = parse_month((start - timedelta(days=1)).strftime("%Y-%m"))
    prev_total = month_totals(prev_start, prev_end)["expense"]
    if prev_total > 0:
        change = (total - prev_total) / prev_total * 100
        word = "up" if change > 0 else "down"
        messages.append(f"Spending is {word} {abs(change):.0f}% compared with last month.")

    # Simple forecast for the current month
    today = date.today()
    if start <= today < end:
        days_elapsed = max((today - start).days + 1, 1)
        forecast = total / days_elapsed * days_in_month
        messages.append(f"At this pace you will spend about Rs. {forecast:,.0f} this month.")

    return jsonify({"insights": messages})


# ----------------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------------
def export_dataframe(month):
    start, end = parse_month(month)
    rows = user_transactions(start, end).order_by(Transaction.date).all()
    df = pd.DataFrame([r.to_dict() for r in rows],
                      columns=["date", "type", "category", "description", "amount"])
    return df, start.strftime("%Y-%m")


@app.route("/export/excel")
@login_required
def export_excel():
    df, label = export_dataframe(request.args.get("month"))
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Transactions")
        if not df.empty:
            summary = df[df["type"] == "expense"].groupby("category")["amount"].sum().reset_index()
            summary.to_excel(writer, index=False, sheet_name="Category Summary")
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"spendwise_{label}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export/csv")
@login_required
def export_csv():
    df, label = export_dataframe(request.args.get("month"))
    buffer = io.BytesIO(df.to_csv(index=False).encode("utf-8"))
    return send_file(buffer, as_attachment=True, download_name=f"spendwise_{label}.csv",
                     mimetype="text/csv")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "1") == "1")
    