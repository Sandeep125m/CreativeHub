from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta,timezone
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client

import os
from dotenv import load_dotenv

load_dotenv()  # loads .env

SECRET_KEY = os.getenv("SECRET_KEY", "fallbacksecret")
DATABASE_URL = os.getenv("DATABASE_URL")

app = Flask(__name__)

app.secret_key = SECRET_KEY


# configure SQL Alchemy
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(100), nullable=True)
    whatsapp_number = db.Column(db.String(20), nullable=True)  # new field
    credits = db.Column(db.Integer, default=100)  # default credits for demo
    expiring_credits = db.Column(db.Integer, default=0)
    expiry_date = db.Column(db.String(50), default="2025-12-01")
    active_requests = db.Column(db.Integer, default=0)
    completed_requests_month = db.Column(db.Integer, default=0)
    credits_used_total = db.Column(db.Integer, default=0)


class ServiceRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    service_type = db.Column(db.String(50), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default="Pending")  # Pending, In Progress, Completed
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    user = db.relationship('User', backref='requests')

class CreditTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    type = db.Column(db.String(50), nullable=False)   # e.g. 'purchase', 'use', 'expiry'
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    expiry_date = db.Column(db.String(50), nullable=True)

    user = db.relationship('User', backref='credit_transactions')



# Create database tables if they don't exist
with app.app_context():
    db.create_all()

# Twilio WhatsApp function
def send_whatsapp(to_number, message):
    if not to_number:
        return
    try:
        account_sid = 'YOUR_TWILIO_SID'
        auth_token = 'YOUR_TWILIO_AUTH_TOKEN'
        client = Client(account_sid, auth_token)
        client.messages.create(
            from_='whatsapp:+14155238886',  # Twilio sandbox number
            body=message,
            to=f'whatsapp:{to_number}'
        )
    except Exception as e:
        print(f"WhatsApp notification failed: {e}")

def update_request_statuses():
    with app.app_context():
        now = datetime.now(timezone.utc)  # timezone-aware current time

        # Fetch pending requests
        pending_requests = ServiceRequest.query.filter_by(status="Pending").all()
        for req in pending_requests:
            # Make created_at timezone-aware if naive
            created_at = req.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at + timedelta(minutes=1) <= now:
                req.status = "In Progress"
                send_whatsapp(req.user.whatsapp_number,
                              f"Your request '{req.title}' is now In Progress.")

        # Fetch in-progress requests
        inprogress_requests = ServiceRequest.query.filter_by(status="In Progress").all()
        for req in inprogress_requests:
            created_at = req.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at + timedelta(minutes=10) <= now:
                req.status = "Completed"
                send_whatsapp(req.user.whatsapp_number,
                              f"Your request '{req.title}' has been Completed.")

        db.session.commit()

scheduler = BackgroundScheduler()
scheduler.add_job(func=update_request_statuses, trigger="interval", seconds=30)
scheduler.start()


def check_and_expire_credits(user):
    """
    Checks if the user's credits have expired and deducts them if necessary.
    Returns True if credits expired, else False.
    """
    if not user.expiry_date:
        return False  # No expiry set

    try:
        expiry_date = datetime.strptime(user.expiry_date, "%Y-%m-%d").date()
    except ValueError:
        # handle old date formats if any
        try:
            expiry_date = datetime.strptime(user.expiry_date, "%m/%d/%Y").date()
        except ValueError:
            return False

    today = datetime.now().date()

    if today > expiry_date and user.credits > 0:
        expired_amount = user.credits

        # Deduct all remaining credits
        user.credits = 0
        user.expiring_credits = 0  # reset
        db.session.commit()

        # Log this expiry
        transaction = CreditTransaction(
            user_id=user.id,
            type='expiry',
            description=f"{expired_amount} credits expired on {expiry_date}",
            amount=-expired_amount,
            expiry_date=user.expiry_date
        )
        db.session.add(transaction)
        db.session.commit()

        return True

    return False


@app.route('/')
def home():
    return render_template('home.html')

@app.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='favicon.ico'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            session['email'] = user.email
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password', 'danger')
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email'].strip().lower()
        password = request.form['password']

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email already exists. Please login.', 'warning')
            return redirect(url_for('login'))

        hashed_pw = generate_password_hash(password)
        new_user = User(email=email, password=hashed_pw, name=name)
        db.session.add(new_user)
        db.session.commit()

        flash('Account created successfully! Please login.', 'success')
        return redirect(url_for('login'))

    return render_template('signup.html')


@app.route('/services')
def services():
    return render_template('services.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('home'))


@app.route('/about')
def about():
    return render_template('about.html')


# Pricing plans
plans = [
    {
        "name": "Starter Package",
        "price": 299,
        "credits": 50,
        "price_per_credit": 5.98,
        "features": [
            "50 service credits",
            "Valid for 3 months",
            "Basic support",
            "WhatsApp notifications",
            "2 revisions per request"
        ]
    },
    {
        "name": "Professional Package",
        "price": 799,
        "credits": 150,
        "price_per_credit": 5.33,
        "popular": True,
        "features": [
            "150 service credits",
            "Valid for 6 months",
            "Priority support",
            "WhatsApp notifications",
            "Unlimited revisions",
            "48-hour turnaround"
        ]
    },
    {
        "name": "Enterprise Package",
        "price": 1899,
        "credits": 400,
        "price_per_credit": 4.75,
        "features": [
            "400 service credits",
            "Valid for 12 months",
            "24/7 priority support",
            "WhatsApp & Slack notifications",
            "Unlimited revisions",
            "24-hour turnaround",
            "Dedicated account manager"
        ]
    }
]

# Top-up credit packs
topups = [
    { "credits": 10, "cost": 69, "bonus": 0 },
    { "credits": 27, "cost": 159, "bonus": 2 },
    { "credits": 55, "cost": 299, "bonus": 5 },
    { "credits": 115, "cost": 549, "bonus": 15 },
]

@app.route("/pricing")
def pricing():
    return render_template("pricing.html", plans=plans, topups=topups)


# Define the route for the main dashboard page
@app.route('/dashboard')
def dashboard():
    # Require login
    if 'user_id' not in session:
        flash('Please login first.', 'info')
        return redirect(url_for('login'))
    
    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        flash('User not found. Please login again.', 'danger')
        return redirect(url_for('login'))

    # Get user info
    user = User.query.get(session['user_id'])

    # 🕒 Expiry check right after login
    if check_and_expire_credits(user):
        flash('Your expired credits have been removed.', 'warning')

    # Calculate credits used total (from transactions if needed)
    credits_used_total = user.credits_used_total or 0

    # Optionally double-check with transactions table (for accuracy)
    credits_used_from_txn = db.session.query(
        db.func.sum(CreditTransaction.amount)
    ).filter(
        CreditTransaction.user_id == user.id,
        CreditTransaction.type == 'use'
    ).scalar()

    # Convert negative sum to positive number
    if credits_used_from_txn:
        credits_used_total = abs(credits_used_from_txn)

    # Fallback values if new user has no data yet
    user_name = user.name or user.email.split('@')[0].capitalize()
    available_credits = user.credits
    expiring_credits = user.expiring_credits
    expiry_date = user.expiry_date
    active_requests = ServiceRequest.query.filter_by(user_id=user.id, status='Pending').count()
    completed_requests_month =ServiceRequest.query.filter_by(user_id=user.id, status='Completed').count()


    # Pass user data to dashboard template
    return render_template(
        'dashboard.html',
        user_name=user_name,
        available_credits=available_credits,
        expiring_credits=expiring_credits,
        expiry_date=expiry_date,
        active_requests=active_requests,
        completed_requests_month=completed_requests_month,
        credits_used_total=credits_used_total
    )


SERVICE_CREDIT_COST = {
    "logo": 5,     # Logo Design costs 5 credits
    "banner": 3,   # Website Banner costs 3 credits
    "social": 2,   # Social Media Post costs 2 credits
    "edit": 4      # Photo Editing costs 4 credits
}

@app.route('/new_request', methods=['GET', 'POST'])
def new_request():
    if 'user_id' not in session:
        flash('Please login first.', 'info')
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    available_credits = user.credits

    if request.method == 'POST':
        service_type = request.form.get('service_type')
        title = request.form.get('request_title')
        description = request.form.get('description')

        if not service_type or not title or not description:
            flash('All fields are required!', 'danger')
            return redirect(url_for('new_request'))

        # Check if user has enough credits
        required_credits = SERVICE_CREDIT_COST.get(service_type, 0)
        if user.credits < required_credits:
            flash(f'You do not have enough credits for this service. Required: {required_credits}', 'danger')
            return redirect(url_for('new_request'))

        # Deduct credits
        user.credits -= required_credits

        # Create new service request
        new_req = ServiceRequest(
            user_id=user.id,
            service_type=service_type,
            title=title,
            description=description
        )
        db.session.add(new_req)

        # Log this usage in the CreditTransaction table
        usage_transaction = CreditTransaction(
            user_id=user.id,
            type='use',
            description=f"Used {required_credits} credits for {service_type.capitalize()} request: {title}",
            amount=-required_credits  # negative value to represent deduction
        )
        db.session.add(usage_transaction)
        db.session.commit()

        flash(f'Your request has been submitted! {required_credits} credits deducted.', 'success')
        return redirect(url_for('my_requests'))

    return render_template('new_request.html', available_credits=available_credits)

@app.route('/my_requests')
def my_requests():
    # Static data for the request status boxes (based on your image)
    user = User.query.get(session['user_id'])
    available_credits = user.credits

    # Fetch all requests for this user (latest first)
    requests_list = (
        ServiceRequest.query.filter_by(user_id=user.id)
        .order_by(ServiceRequest.created_at.desc())
        .all()
    )

    # Calculate request status counts for dashboard summary
    # Compute request status counts
    statuses = {
        'pending': 0,
        'in_progress': 0,
        'completed': 0,
        'cancelled': 0
    }

    for req in requests_list:
        key = req.status.lower().replace(" ", "_")  # normalize status
        if key in statuses:
            statuses[key] += 1
        else:
            # In case there are unexpected statuses
            statuses[key] = 1

    return render_template(
        'my_request.html',
        available_credits=available_credits,
        requests_list=requests_list,
        statuses=statuses,
        SERVICE_CREDIT_COST=SERVICE_CREDIT_COST
    )

@app.route('/cancel_request/<int:request_id>', methods=['POST'])
def cancel_request(request_id):
    if 'user_id' not in session:
        flash('Please login first.', 'info')
        return redirect(url_for('login'))
    req = ServiceRequest.query.get(request_id)
    if not req or req.user_id != session['user_id']:
        flash('Request not found.', 'danger')
        return redirect(url_for('my_requests'))

    req.status = "Cancelled"
    db.session.commit()
    send_whatsapp(req.user.whatsapp_number, f"Your request '{req.title}' has been cancelled.")
    flash('Request cancelled successfully!', 'success')
    return redirect(url_for('my_requests'))


@app.route('/buy_package', methods=['GET', 'POST'])
def buy_package():
    if 'user_id' not in session:
        flash('Please login first.', 'info')
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    available_credits = user.credits

    # Prepare package data
    packages = [
        {
            "name": "Starter Package",
            "price": 299,
            "credits": 50,
            "price_per_credit": 5.98,
            "description": "Perfect for small creative needs.",
            "features": [
                "50 service credits",
                "Valid for 3 months",
                "Basic support",
                "WhatsApp notifications",
                "2 revisions per request"
            ],
            "is_popular": False,
            "is_selected": True  # default selection
        },
        {
            "name": "Professional Package",
            "price": 799,
            "credits": 150,
            "price_per_credit": 5.33,
            "description": "Ideal for growing creative teams.",
            "features": [
                "150 service credits",
                "Valid for 6 months",
                "Priority support",
                "WhatsApp notifications",
                "Unlimited revisions",
                "48-hour turnaround"
            ],
            "is_popular": True,
            "is_selected": False
        },
        {
            "name": "Enterprise Package",
            "price": 1899,
            "credits": 400,
            "price_per_credit": 4.75,
            "description": "For high-volume creative workflows.",
            "features": [
                "400 service credits",
                "Valid for 12 months",
                "24/7 priority support",
                "WhatsApp & Slack notifications",
                "Unlimited revisions",
                "24-hour turnaround",
                "Dedicated account manager"
            ],
            "is_popular": False,
            "is_selected": False
        }
    ]

    # Handle package purchase (POST)
    if request.method == 'POST':
        selected_name = request.form.get('selected_package')

        # Find the selected package
        selected_package = next((p for p in packages if p["name"] == selected_name), None)
        if not selected_package:
            flash('Invalid package selection.', 'danger')
            return redirect(url_for('buy_package'))

        # Update user credits
        user.credits += selected_package["credits"]

        # Set expiry date 1 year from now
        expiry_date = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
        user.expiry_date = expiry_date

        # Create credit transaction record
        transaction = CreditTransaction(
            user_id=user.id,
            type='purchase',
            description=f"Purchased {selected_package['name']} package",
            amount=selected_package["credits"],
            expiry_date=expiry_date
        )

        db.session.add(transaction)
        db.session.commit()

        flash(f'You have successfully purchased the {selected_name}! {selected_package["credits"]} credits added.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('buy_package.html', packages=packages, available_credits=available_credits)
    


@app.route('/buy_credits',methods=["GET", "POST"])
def buy_credits():
    if 'user_id' not in session:
        flash('Please login first.', 'info')
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    available_credits = user.credits

    # Available top-up packs
    credit_packs = [
        {"credits": 10, "cost": 69, "bonus": 0},
        {"credits": 27, "cost": 159, "bonus": 2},
        {"credits": 55, "cost": 299, "bonus": 5},
        {"credits": 115, "cost": 549, "bonus": 15}
    ]

    if request.method == 'POST':
        selected_credits = int(request.form.get('credits'))
        selected_bonus = int(request.form.get('bonus'))
        selected_cost = float(request.form.get('cost'))

        total_credits = selected_credits + selected_bonus
        expiry_date = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")

        # Update user credits and expiry
        user.credits += total_credits
        user.expiry_date = expiry_date

        # Log purchase
        transaction = CreditTransaction(
            user_id=user.id,
            type='purchase',
            description=f"Purchased {selected_credits} credits (+{selected_bonus} bonus)",
            amount=total_credits,
            expiry_date=expiry_date
        )

        db.session.add(transaction)
        db.session.commit()

        flash(f'Purchase successful! {total_credits} credits added to your account.', 'success')
        return redirect(url_for('dashboard'))

    return render_template(
        'buy_credits.html',
        available_credits=available_credits,
        credit_packs=credit_packs
    )


@app.route('/credit_history')
def credit_history():
    if 'user_id' not in session:
        flash('Please login first.', 'info')
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    transactions = CreditTransaction.query.filter_by(user_id=user.id).order_by(CreditTransaction.created_at.desc()).all()
    total_purchased = sum(t.amount for t in transactions if t.amount > 0)
    total_used = sum(-t.amount for t in transactions if t.amount < 0)
    current_balance = user.credits

    return render_template(
        'credit_history.html',
        available_credits=current_balance,
        total_purchased=total_purchased,
        total_used=total_used,
        transactions=transactions
    )

@app.route('/settings', methods=['GET', 'POST'])
def setting():
    if 'user_id' not in session:
        flash('Please login first.', 'info')
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])

    if request.method == 'POST':
        new_number = request.form.get('whatsapp_number').strip()

        if not new_number:
            flash('Please enter a valid WhatsApp number.', 'danger')
            return redirect(url_for('whatsapp'))

        user.whatsapp_number = new_number
        db.session.commit()
        flash('Your WhatsApp number has been updated!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('setting.html', whatsapp_number=user.whatsapp_number)


if __name__ == '__main__':
    app.run(debug=True)
