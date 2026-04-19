import os
import json
import logging
import base64
import requests
import uuid
import qrcode
import re
import hashlib
from io import BytesIO
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, make_response
from flask_sqlalchemy import SQLAlchemy

from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.lib.utils import ImageReader
from PIL import Image as PILImage

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Ensure the instance folder exists
try:
    os.makedirs(app.instance_path)
    logger.info(f"Instance folder created at: {app.instance_path}")
except OSError:
    logger.info(f"Instance folder already exists at: {app.instance_path}")

# Set database path to instance folder
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'tickets.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
logger.info(f"Database will be created at: {os.path.join(app.instance_path, 'tickets.db')}")

db = SQLAlchemy(app)

# Token serializer for password reset
app.config['SECURITY_PASSWORD_SALT'] = os.environ.get('SECURITY_PASSWORD_SALT', 'password-reset-salt')
serializer = URLSafeTimedSerializer(app.secret_key)

# Mock Payment Configuration
USE_MOCK_PAYMENTS = True

# PayMongo Configuration
PAYMONGO_SECRET_KEY = os.environ.get('PAYMONGO_SECRET_KEY', '')
PAYMONGO_PUBLIC_KEY = os.environ.get('PAYMONGO_PUBLIC_KEY', '')
PAYMONGO_API_URL = "https://api.paymongo.com/v1"

# Base URL for receipts
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')

# Template filter for datetime formatting
@app.template_filter('datetimeformat')
def datetimeformat(value, format='%Y-%m-%d %H:%M'):
    if value is None:
        return 'N/A'
    return value.strftime(format)

# ====================
# Database Models
# ====================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    profile_picture = db.Column(db.String(200), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    address = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    reset_token = db.Column(db.String(200), nullable=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<User {self.email}>'

class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    passenger_name = db.Column(db.String(100), nullable=False)
    passenger_email = db.Column(db.String(120), nullable=False)
    passenger_phone = db.Column(db.String(20), nullable=True)
    route = db.Column(db.String(200), nullable=False)
    departure_date = db.Column(db.String(50), nullable=False)
    departure_time = db.Column(db.String(50), default="08:00 AM")
    return_date = db.Column(db.String(50), nullable=True)
    return_time = db.Column(db.String(50), nullable=True)
    trip_type = db.Column(db.String(20), nullable=False, default="one-way")
    amount = db.Column(db.Float, nullable=False, default=0.0)
    payment_status = db.Column(db.String(50), default="pending")
    payment_method = db.Column(db.String(50), nullable=True)
    payment_intent_id = db.Column(db.String(100), nullable=True)
    checkout_session_id = db.Column(db.String(100), nullable=True)
    checkout_url = db.Column(db.String(500), nullable=True)
    reference_number = db.Column(db.String(50), unique=True, nullable=True)
    status = db.Column(db.String(50), default="active")
    seat_number = db.Column(db.String(20), nullable=True)
    special_requests = db.Column(db.Text, nullable=True)
    verification_code = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('tickets', lazy=True))

    def __repr__(self):
        return f'<Ticket {self.reference_number}>'

class FerryRoute(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    origin = db.Column(db.String(100), nullable=False)
    destination = db.Column(db.String(100), nullable=False)
    one_way_price = db.Column(db.Float, nullable=False, default=500.0)
    round_trip_price = db.Column(db.Float, nullable=False, default=900.0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<FerryRoute {self.origin} → {self.destination}>'
    
    @property
    def route_name(self):
        return f"{self.origin} → {self.destination}"

class FerrySchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey('ferry_route.id'), nullable=False)
    departure_time = db.Column(db.String(50), nullable=False)
    arrival_time = db.Column(db.String(50), nullable=False)
    duration = db.Column(db.String(50), nullable=True)
    vessel_name = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    days_of_week = db.Column(db.String(200), default="Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday")
    
    route = db.relationship('FerryRoute', backref=db.backref('schedules', lazy=True))
    
    def __repr__(self):
        return f'<FerrySchedule {self.departure_time}>'

class DisabledDate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey('ferry_route.id'), nullable=True)
    date = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    route = db.relationship('FerryRoute', backref=db.backref('disabled_dates', lazy=True))
    
    def __repr__(self):
        route_name = "ALL ROUTES" if not self.route_id else f"{self.route.origin}→{self.route.destination}"
        return f'<DisabledDate {route_name}: {self.date}>'

# ====================
# QR Code Functions
# ====================

def generate_qr_code_for_pdf(ticket):
    """Generate unique QR code image for PDF receipt"""
    # Generate a unique verification code if not exists
    if not ticket.verification_code:
        unique_string = f"{ticket.reference_number}{ticket.id}{ticket.passenger_email}{ticket.created_at}"
        hash_object = hashlib.md5(unique_string.encode())
        ticket.verification_code = f"SHIP{int(hash_object.hexdigest()[:8], 16) % 10000000:07d}"
        # Don't commit here - let the caller handle it
    
    # Create comprehensive QR data for verification
    qr_data = f"""SHIPPYDASH FERRY TICKET
═══════════════════════════
Reference: {ticket.reference_number}
Verification: {ticket.verification_code}
Passenger: {ticket.passenger_name}
Route: {ticket.route}
Departure: {ticket.departure_date} at {ticket.departure_time}
Trip Type: {ticket.trip_type.upper()}
Amount: ₱{ticket.amount:.2f}
Status: {ticket.payment_status.upper()}
═══════════════════════════
Scan to verify ticket validity"""
    
    qr = qrcode.QRCode(
        version=5,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    
    # Create QR code image with styling
    img = qr.make_image(fill_color="#0056a6", back_color="white")
    
    buffered = BytesIO()
    img.save(buffered, format="PNG", quality=95)
    buffered.seek(0)
    return buffered

def generate_ticket_receipt(ticket):
    """Generate a PDF receipt for the ticket with prominent QR code"""
    buffer = BytesIO()
    
    # Generate verification code if needed
    if not ticket.verification_code:
        unique_string = f"{ticket.reference_number}{ticket.id}{ticket.passenger_email}{ticket.created_at}"
        hash_object = hashlib.md5(unique_string.encode())
        ticket.verification_code = f"SHIP{int(hash_object.hexdigest()[:8], 16) % 10000000:07d}"
    
    # Create the PDF document
    doc = SimpleDocTemplate(buffer, pagesize=A4, 
                           rightMargin=72, leftMargin=72,
                           topMargin=72, bottomMargin=18)
    
    # Container for the 'Flowable' objects
    elements = []
    
    # Styles
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Center', alignment=TA_CENTER))
    styles.add(ParagraphStyle(name='Right', alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name='BoldCenter', alignment=TA_CENTER, fontName='Helvetica-Bold', fontSize=14))
    
    # Add logo/title
    elements.append(Paragraph("SHIPPYDASH", styles['Title']))
    elements.append(Paragraph("FerrySafe. FerryFast. FerryConvenient.", styles['Center']))
    elements.append(Spacer(1, 0.15*inch))
    
    # Add receipt header
    header_style = ParagraphStyle(
        name='ReceiptHeader',
        parent=styles['Heading1'],
        alignment=TA_CENTER,
        textColor=colors.HexColor('#0056a6'),
        fontSize=18,
        spaceAfter=10
    )
    elements.append(Paragraph("OFFICIAL TICKET & RECEIPT", header_style))
    elements.append(Spacer(1, 0.1*inch))
    
    # Receipt info
    receipt_data = [
        ["Ticket Number:", f"#{ticket.reference_number}"],
        ["Verification Code:", f"{ticket.verification_code}"],
        ["Issue Date:", datetime.now().strftime("%B %d, %Y at %I:%M %p")],
        ["Payment Status:", "PAID" if ticket.payment_status == 'paid' else ticket.payment_status.upper()],
    ]
    
    receipt_table = Table(receipt_data, colWidths=[2*inch, 4*inch])
    receipt_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(receipt_table)
    elements.append(Spacer(1, 0.2*inch))
    
    # Add horizontal line
    elements.append(Paragraph("─" * 70, styles['Center']))
    elements.append(Spacer(1, 0.15*inch))
    
    # Passenger Information
    elements.append(Paragraph("PASSENGER INFORMATION", styles['Heading2']))
    elements.append(Spacer(1, 0.05*inch))
    
    passenger_data = [
        ["Name:", ticket.passenger_name],
        ["Email:", ticket.passenger_email],
        ["Phone:", ticket.passenger_phone or "Not provided"],
    ]
    
    passenger_table = Table(passenger_data, colWidths=[1.5*inch, 4.5*inch])
    passenger_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f0f8ff')),
    ]))
    elements.append(passenger_table)
    elements.append(Spacer(1, 0.15*inch))
    
    # Trip Information
    elements.append(Paragraph("TRIP INFORMATION", styles['Heading2']))
    elements.append(Spacer(1, 0.05*inch))
    
    trip_data = [
        ["Route:", ticket.route],
        ["Trip Type:", ticket.trip_type.upper()],
        ["Departure Date:", ticket.departure_date],
        ["Departure Time:", ticket.departure_time],
    ]
    
    if ticket.trip_type == 'round-trip' and ticket.return_date:
        trip_data.append(["Return Date:", ticket.return_date])
        trip_data.append(["Return Time:", ticket.return_time or "06:00 PM"])
    
    if ticket.seat_number:
        trip_data.append(["Seat Number:", ticket.seat_number])
    
    trip_table = Table(trip_data, colWidths=[1.5*inch, 4.5*inch])
    trip_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f0f8ff')),
    ]))
    elements.append(trip_table)
    elements.append(Spacer(1, 0.15*inch))
    
    # Payment Information
    elements.append(Paragraph("PAYMENT INFORMATION", styles['Heading2']))
    elements.append(Spacer(1, 0.05*inch))
    
    payment_data = [
        ["Amount Paid:", f"₱{ticket.amount:,.2f}"],
        ["Payment Method:", ticket.payment_method or "Online Payment"],
        ["Transaction Date:", datetime.now().strftime("%B %d, %Y at %I:%M %p")],
    ]
    
    payment_table = Table(payment_data, colWidths=[1.5*inch, 4.5*inch])
    payment_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f0f8ff')),
        ('TEXTCOLOR', (1, 0), (1, 0), colors.HexColor('#28a745')),
        ('FONTWEIGHT', (1, 0), (1, 0), 'Bold'),
    ]))
    elements.append(payment_table)
    elements.append(Spacer(1, 0.2*inch))
    
    # Add horizontal line
    elements.append(Paragraph("─" * 70, styles['Center']))
    elements.append(Spacer(1, 0.2*inch))
    
    # Generate QR Code
    qr_buffer = generate_qr_code_for_pdf(ticket)
    
    # Create an Image flowable from the QR code buffer
    qr_img = Image(qr_buffer, width=2*inch, height=2*inch)
    
    # Create verification text
    verification_text = f"""
    <b>✓ VERIFICATION INFORMATION</b><br/>
    <br/>
    <b>Verification Code:</b> {ticket.verification_code}<br/>
    <br/>
    <b>How to verify:</b><br/>
    1. Scan this QR code at the boarding gate<br/>
    2. Present your valid ID<br/>
    3. Have your reference number ready<br/>
    <br/>
    <b>Important:</b><br/>
    • This ticket is valid only for the stated date and time<br/>
    • Please arrive 2 hours before departure<br/>
    • Keep this ticket for your return journey
    """
    
    # Create a table for QR code and text side by side
    qr_table_data = [
        [qr_img, Paragraph(verification_text, styles['Normal'])]
    ]
    
    qr_table = Table(qr_table_data, colWidths=[2.2*inch, 3.3*inch])
    qr_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('ALIGN', (1, 0), (1, 0), 'LEFT'),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#0056a6')),
        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#ffffff')),
        ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#f8f9fa')),
        ('PADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(qr_table)
    elements.append(Spacer(1, 0.2*inch))
    
    # Important Notes
    elements.append(Paragraph("IMPORTANT NOTES:", styles['Heading3']))
    notes = [
        "• Please arrive at the port at least 2 hours before departure.",
        "• Present this receipt (printed or digital) at the boarding gate.",
        "• Valid ID is required for verification.",
        "• Baggage allowance: 20kg per passenger.",
        "• This receipt is proof of payment and serves as your ticket.",
        f"• Verification Code: {ticket.verification_code} (for online verification)"
    ]
    
    for note in notes:
        elements.append(Paragraph(note, styles['Normal']))
        elements.append(Spacer(1, 0.05*inch))
    
    elements.append(Spacer(1, 0.3*inch))
    
    # Footer
    elements.append(Paragraph("Thank you for choosing ShippyDash!", styles['Center']))
    elements.append(Paragraph("© 2026 ShippyDash. All Rights Reserved", styles['Italic']))
    
    # Build PDF
    doc.build(elements)
    
    buffer.seek(0)
    return buffer

# ====================
# Admin Required Decorator
# ====================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page.', 'error')
            return redirect(url_for('login'))
        
        user = User.query.get(session['user_id'])
        if not user or not user.is_admin:
            flash('You do not have permission to access this page.', 'error')
            return redirect(url_for('home'))
        
        return f(*args, **kwargs)
    return decorated_function

# ====================
# Receipt Routes
# ====================

@app.route('/receipt/<int:ticket_id>')
def download_receipt(ticket_id):
    """Download receipt as PDF with QR code"""
    if 'user_id' not in session:
        flash('Please login to download receipts.', 'error')
        return redirect(url_for('login'))
    
    ticket = Ticket.query.get_or_404(ticket_id)
    
    # Check permission
    if ticket.passenger_email != session['user_email'] and not session.get('is_admin'):
        flash('You do not have permission to download this receipt.', 'error')
        return redirect(url_for('tickets'))
    
    # Check if ticket is paid
    if ticket.payment_status != 'paid':
        flash('Receipt is only available for paid tickets.', 'error')
        return redirect(url_for('ticket_detail', ticket_id=ticket.id))
    
    try:
        # Generate PDF receipt
        pdf_buffer = generate_ticket_receipt(ticket)
        
        # Save verification code if newly generated
        db.session.commit()
        
        # Create response
        response = make_response(pdf_buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename=ShippyDash_Receipt_{ticket.reference_number}.pdf'
        
        return response
        
    except Exception as e:
        logger.error(f"Receipt generation error: {str(e)}")
        flash('Error generating receipt. Please try again.', 'error')
        return redirect(url_for('ticket_detail', ticket_id=ticket.id))

@app.route('/receipt/view/<int:ticket_id>')
def view_receipt(ticket_id):
    """View receipt in browser with QR code"""
    if 'user_id' not in session:
        flash('Please login to view receipts.', 'error')
        return redirect(url_for('login'))
    
    ticket = Ticket.query.get_or_404(ticket_id)
    
    # Check permission
    if ticket.passenger_email != session['user_email'] and not session.get('is_admin'):
        flash('You do not have permission to view this receipt.', 'error')
        return redirect(url_for('tickets'))
    
    # Check if ticket is paid
    if ticket.payment_status != 'paid':
        flash('Receipt is only available for paid tickets.', 'error')
        return redirect(url_for('ticket_detail', ticket_id=ticket.id))
    
    try:
        # Generate PDF receipt
        pdf_buffer = generate_ticket_receipt(ticket)
        
        # Save verification code if newly generated
        db.session.commit()
        
        # Create response for viewing in browser
        response = make_response(pdf_buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'inline; filename=ShippyDash_Receipt_{ticket.reference_number}.pdf'
        
        return response
        
    except Exception as e:
        logger.error(f"Receipt generation error: {str(e)}")
        flash('Error generating receipt. Please try again.', 'error')
        return redirect(url_for('ticket_detail', ticket_id=ticket.id))

# ====================
# Mock Payment Functions
# ====================
def create_mock_payment_intent(amount, description, email, name):
    """Create a mock payment intent"""
    payment_intent_id = f"mock_pi_{uuid.uuid4().hex[:12]}"
    
    logger.info(f"[MOCK] Creating payment intent: {payment_intent_id} for ₱{amount}")
    
    return {
        "data": {
            "id": payment_intent_id,
            "type": "payment_intent",
            "attributes": {
                "amount": int(amount * 100),
                "currency": "PHP",
                "description": description,
                "status": "pending",
                "metadata": {
                    "customer_email": email,
                    "customer_name": name
                }
            }
        }
    }

def create_mock_checkout_session(payment_intent_id, success_url, cancel_url):
    """Create a mock checkout session"""
    checkout_id = f"mock_cs_{uuid.uuid4().hex[:12]}"
    
    mock_checkout_url = url_for('mock_payment_page', payment_intent_id=payment_intent_id, _external=True)
    
    logger.info(f"[MOCK] Creating checkout session: {checkout_id}")
    logger.info(f"[MOCK] Checkout URL: {mock_checkout_url}")
    
    return {
        "data": {
            "id": checkout_id,
            "type": "checkout_session",
            "attributes": {
                "payment_intent_id": payment_intent_id,
                "checkout_url": mock_checkout_url,
                "status": "active"
            }
        }
    }

@app.route('/mock-payment/<payment_intent_id>')
def mock_payment_page(payment_intent_id):
    """Mock payment page that simulates a payment gateway"""
    ticket = Ticket.query.filter_by(payment_intent_id=payment_intent_id).first()
    
    if not ticket:
        flash('Ticket not found.', 'error')
        return redirect(url_for('tickets'))
    
    # Pass a flag to the template to know if this is from kiosk
    is_kiosk = ticket.reference_number and ticket.reference_number.startswith('KSK')
    
    return render_template('mock_payment.html', ticket=ticket, payment_intent_id=payment_intent_id, is_kiosk=is_kiosk)

@app.route('/mock-payment/process/<int:ticket_id>', methods=['POST'])
def mock_process_payment(ticket_id):
    """Process mock payment"""
    ticket = Ticket.query.get_or_404(ticket_id)
    
    logger.info(f"[MOCK] Processing payment for ticket {ticket.reference_number}")
    
    ticket.payment_status = 'paid'
    ticket.payment_method = 'Mock Payment'
    db.session.commit()
    
    flash('Payment successful! Your ticket is confirmed.', 'success')
    
    # Check if this is a kiosk ticket (reference starts with KSK)
    if ticket.reference_number and ticket.reference_number.startswith('KSK'):
        return redirect(url_for('kiosk_payment_success', ticket_id=ticket.id))
    else:
        return redirect(url_for('payment_success', ticket_id=ticket.id))

# ====================
# PayMongo Helpers (Fallback)
# ====================
def get_auth_header():
    """Generate Basic Auth header for PayMongo"""
    auth_string = f"{PAYMONGO_SECRET_KEY}:"
    auth_bytes = auth_string.encode('utf-8')
    base64_bytes = base64.b64encode(auth_bytes)
    base64_auth = base64_bytes.decode('utf-8')
    
    return {
        "Authorization": f"Basic {base64_auth}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def create_payment_intent(amount, description, email, name):
    """Create a payment intent - uses mock if enabled"""
    if USE_MOCK_PAYMENTS:
        return create_mock_payment_intent(amount, description, email, name)
    
    headers = get_auth_header()
    amount_in_centavos = int(abs(amount) * 100)
    
    payload = {
        "data": {
            "attributes": {
                "amount": amount_in_centavos,
                "payment_method_allowed": ["gcash", "card", "paymaya"],
                "currency": "PHP",
                "description": description[:50],
                "statement_descriptor": "ShippyDash",
                "metadata": {
                    "customer_email": email,
                    "customer_name": name
                }
            }
        }
    }
    
    try:
        logger.info(f"Creating payment intent for ₱{amount}")
        response = requests.post(
            f"{PAYMONGO_API_URL}/payment_intents",
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if response.status_code in [200, 201]:
            return response.json()
        else:
            error_data = response.json() if response.text else {"errors": [{"detail": "Unknown error"}]}
            logger.error(f"Payment intent error: {error_data}")
            return {"error": error_data, "status_code": response.status_code}
            
    except Exception as e:
        logger.error(f"Payment intent error: {str(e)}")
        return {"error": {"errors": [{"detail": str(e)}]}}

def create_checkout_session(payment_intent_id, success_url, cancel_url):
    """Create a checkout session - uses mock if enabled"""
    if USE_MOCK_PAYMENTS:
        return create_mock_checkout_session(payment_intent_id, success_url, cancel_url)
    
    headers = get_auth_header()
    
    payload = {
        "data": {
            "attributes": {
                "payment_intent_id": payment_intent_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "send_email_receipt": False,
                "show_description": True,
                "show_line_items": True
            }
        }
    }
    
    try:
        logger.info(f"Creating checkout session for PI: {payment_intent_id}")
        response = requests.post(
            f"{PAYMONGO_API_URL}/checkout_sessions",
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if response.status_code in [200, 201]:
            return response.json()
        else:
            error_data = response.json() if response.text else {"errors": [{"detail": "Unknown error"}]}
            error_message = "Unknown error"
            if 'errors' in error_data and len(error_data['errors']) > 0:
                error_message = error_data['errors'][0].get('detail', 'Unknown error')
            return {"error": error_message, "status_code": response.status_code}
            
    except Exception as e:
        logger.error(f"Checkout error: {str(e)}")
        return {"error": str(e)}

def get_payment_intent(payment_intent_id):
    """Get payment intent details"""
    if USE_MOCK_PAYMENTS:
        return {
            "data": {
                "id": payment_intent_id,
                "attributes": {
                    "status": "succeeded"
                }
            }
        }
    
    headers = get_auth_header()
    
    try:
        response = requests.get(
            f"{PAYMONGO_API_URL}/payment_intents/{payment_intent_id}",
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Failed to get payment intent: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Get payment intent error: {str(e)}")
        return None

# ====================
# Password Reset Functions
# ====================
def generate_reset_token(email):
    return serializer.dumps(email, salt=app.config['SECURITY_PASSWORD_SALT'])

def verify_reset_token(token, expiration=3600):
    try:
        email = serializer.loads(
            token, 
            salt=app.config['SECURITY_PASSWORD_SALT'],
            max_age=expiration
        )
        return email
    except (SignatureExpired, BadSignature):
        return None

def simulate_send_email(email, reset_url):
    logger.info("=" * 50)
    logger.info("PASSWORD RESET EMAIL (SIMULATED)")
    logger.info(f"To: {email}")
    logger.info(f"Reset URL: {reset_url}")
    logger.info("=" * 50)
    flash(f'[DEV MODE] Reset link: {reset_url}', 'info')
    return True

# ====================
# Main Routes
# ====================
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        errors = []
        
        if not name:
            errors.append('Name is required')
        elif len(name) < 2:
            errors.append('Name must be at least 2 characters long')
        
        if not email:
            errors.append('Email is required')
        elif '@' not in email or '.' not in email:
            errors.append('Please enter a valid email address')
        
        if not password:
            errors.append('Password is required')
        elif len(password) < 6:
            errors.append('Password must be at least 6 characters long')
        
        if password != confirm_password:
            errors.append('Passwords do not match')
        
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            errors.append('An account with this email already exists.')
        
        if errors:
            return render_template('register.html', error='<br>'.join(errors))
        
        try:
            is_first_user = User.query.count() == 0
            
            new_user = User(
                name=name, 
                email=email, 
                password=password,
                is_admin=is_first_user
            )
            db.session.add(new_user)
            db.session.commit()
            
            logger.info(f"New user registered: {email} (Admin: {is_first_user})")
            flash('Registration successful! Please login to continue.', 'success')
            return redirect(url_for('login'))
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Registration error: {str(e)}")
            return render_template('register.html', error='An error occurred during registration. Please try again.')
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        if not email or not password:
            flash('Please enter both email and password', 'error')
            return render_template('login.html')
        
        user = User.query.filter_by(email=email).first()
        
        if not user:
            flash('No account found with this email. Please register below.', 'error')
            return render_template('login.html')
        
        if not user.is_active:
            flash('This account has been banned. Please contact support.', 'error')
            return render_template('login.html')
        
        if user.password != password:
            flash('Incorrect password. Please try again.', 'error')
            return render_template('login.html')
        
        user.last_login = datetime.utcnow()
        db.session.commit()
        
        session.clear()
        session['user_id'] = user.id
        session['user_name'] = user.name
        session['user_email'] = user.email
        session['is_admin'] = user.is_admin
        
        logger.info(f"User logged in: {email}, Admin: {user.is_admin}")
        flash(f'Welcome back, {user.name}!', 'success')
        
        if user.is_admin:
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('tickets'))
    
    return render_template('login.html')

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        
        if not email:
            flash('Please enter your email address.', 'error')
            return render_template('forgot_password.html')
        
        user = User.query.filter_by(email=email).first()
        
        if user:
            token = generate_reset_token(email)
            user.reset_token = token
            user.reset_token_expiry = datetime.utcnow() + timedelta(hours=1)
            db.session.commit()
            
            reset_url = url_for('reset_password', token=token, _external=True)
            simulate_send_email(email, reset_url)
            
            flash('Password reset instructions have been sent to your email.', 'success')
        else:
            flash('If an account exists with this email, you will receive password reset instructions.', 'info')
        
        return redirect(url_for('login'))
    
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    email = verify_reset_token(token)
    
    if not email:
        flash('The password reset link is invalid or has expired.', 'error')
        return redirect(url_for('forgot_password'))
    
    user = User.query.filter_by(email=email).first()
    
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('forgot_password'))
    
    if user.reset_token != token or user.reset_token_expiry < datetime.utcnow():
        flash('The password reset link has expired.', 'error')
        return redirect(url_for('forgot_password'))
    
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        errors = []
        
        if not password:
            errors.append('Password is required')
        elif len(password) < 6:
            errors.append('Password must be at least 6 characters long')
        
        if password != confirm_password:
            errors.append('Passwords do not match')
        
        if errors:
            return render_template('reset_password.html', error='<br>'.join(errors), token=token)
        
        try:
            user.password = password
            user.reset_token = None
            user.reset_token_expiry = None
            db.session.commit()
            
            flash('Your password has been reset successfully.', 'success')
            return redirect(url_for('login'))
            
        except Exception as e:
            logger.error(f"Password reset error: {str(e)}")
            flash('An error occurred. Please try again.', 'error')
            return redirect(url_for('forgot_password'))
    
    return render_template('reset_password.html', token=token)

# ====================
# BOOKING ROUTE
# ====================
@app.route('/book', methods=['GET', 'POST'])
def book():
    if 'user_id' not in session:
        flash('Please login to book a ticket.', 'error')
        return redirect(url_for('login'))
    
    # Get active routes for display
    active_routes = FerryRoute.query.filter_by(is_active=True).all()
    
    # If no routes exist, create default ones
    if not active_routes:
        logger.warning("No routes found in database! Creating default routes...")
        default_routes = [
            FerryRoute(origin='Naval', destination='Cebu', one_way_price=500, round_trip_price=900, is_active=True),
            FerryRoute(origin='Naval', destination='Ormoc', one_way_price=500, round_trip_price=900, is_active=True),
            FerryRoute(origin='Cebu', destination='Naval', one_way_price=500, round_trip_price=900, is_active=True),
            FerryRoute(origin='Cebu', destination='Ormoc', one_way_price=500, round_trip_price=900, is_active=True),
            FerryRoute(origin='Ormoc', destination='Naval', one_way_price=500, round_trip_price=900, is_active=True),
            FerryRoute(origin='Ormoc', destination='Cebu', one_way_price=500, round_trip_price=900, is_active=True),
        ]
        for route in default_routes:
            db.session.add(route)
        db.session.commit()
        active_routes = FerryRoute.query.filter_by(is_active=True).all()
        logger.info(f"Created {len(active_routes)} default routes")
    
    # Get default prices for JavaScript
    default_one_way = 500
    default_round_trip = 900
    if active_routes:
        default_one_way = active_routes[0].one_way_price
        default_round_trip = active_routes[0].round_trip_price
    
    if request.method == 'POST':
        try:
            from_location = request.form.get('from')
            to_location = request.form.get('to')
            departure_date = request.form.get('departure_date')
            return_date = request.form.get('return_date')
            trip_type = request.form.get('trip_type', 'one-way')
            special_requests = request.form.get('special_requests', '')
            departure_schedule_id = request.form.get('departure_schedule')
            return_schedule_id = request.form.get('return_schedule')
            departure_time = request.form.get('departure_time')
            return_time = request.form.get('return_time')
            
            # Log the booking attempt
            logger.info(f"Booking attempt: {from_location} -> {to_location} on {departure_date}")
            
            # Validate required fields
            if not all([from_location, to_location, departure_date]):
                flash('Please fill in all required fields', 'error')
                return redirect(url_for('book'))
            
            if from_location == to_location:
                flash('Departure and destination cannot be the same', 'error')
                return redirect(url_for('book'))
            
            # Get route prices - Check if route exists
            route_config = FerryRoute.query.filter_by(origin=from_location, destination=to_location, is_active=True).first()
            
            # If route doesn't exist, try without is_active filter
            if not route_config:
                route_config = FerryRoute.query.filter_by(origin=from_location, destination=to_location).first()
            
            # If still no route, show error
            if not route_config:
                logger.error(f"Route not found: {from_location} -> {to_location}")
                flash(f'The route from {from_location} to {to_location} is not available. Available routes: Naval-Cebu, Naval-Ormoc, Cebu-Naval, Cebu-Ormoc, Ormoc-Naval, Ormoc-Cebu', 'error')
                return redirect(url_for('book'))
            
            logger.info(f"Found route: {route_config.origin} -> {route_config.destination} (ID: {route_config.id}, Price: {route_config.one_way_price})")
            
            # Get departure schedule details
            if departure_schedule_id and departure_schedule_id != '':
                try:
                    departure_schedule = FerrySchedule.query.get(int(departure_schedule_id))
                    if departure_schedule:
                        departure_time = departure_schedule.departure_time
                        logger.info(f"Using departure schedule: {departure_time}")
                except (ValueError, TypeError):
                    pass
            
            # If no schedule selected, use default
            if not departure_time:
                departure_time = "08:00 AM"
            
            # Calculate amount based on trip type
            if trip_type == 'one-way':
                amount = route_config.one_way_price
                logger.info(f"One-way amount: ₱{amount}")
            else:
                amount = route_config.round_trip_price
                logger.info(f"Round trip amount: ₱{amount}")
                # Get return schedule details
                if return_schedule_id and return_schedule_id != '':
                    try:
                        return_schedule = FerrySchedule.query.get(int(return_schedule_id))
                        if return_schedule:
                            return_time = return_schedule.departure_time
                            logger.info(f"Using return schedule: {return_time}")
                    except (ValueError, TypeError):
                        pass
                if not return_time:
                    return_time = "06:00 PM"
            
            # Create route string for display
            if trip_type == 'round-trip' and return_date:
                route = f"{from_location} ↔ {to_location} (Round Trip)"
            else:
                route = f"{from_location} → {to_location}"
            
            user = User.query.get(session['user_id'])

            if not user:
                session.clear()
                flash('Your session has expired or your account no longer exists. Please log in again.', 'error')
                return redirect(url_for('login'))

            # Create ticket
            ticket = Ticket(
                user_id=user.id,
                passenger_name=user.name,
                passenger_email=user.email,
                passenger_phone=user.phone,
                route=route,
                departure_date=departure_date,
                departure_time=departure_time,
                return_date=return_date if trip_type == 'round-trip' else None,
                return_time=return_time if trip_type == 'round-trip' else None,
                trip_type=trip_type,
                amount=amount,
                payment_status='pending',
                special_requests=special_requests
            )
            db.session.add(ticket)
            db.session.commit()
            
            # Generate reference number and verification code
            ticket.reference_number = f"SD{datetime.now().strftime('%Y%m%d')}{ticket.id:04d}"
            unique_string = f"{ticket.reference_number}{ticket.id}{ticket.passenger_email}{ticket.created_at}"
            hash_object = hashlib.md5(unique_string.encode())
            ticket.verification_code = f"SHIP{int(hash_object.hexdigest()[:8], 16) % 10000000:07d}"
            db.session.commit()
            
            logger.info(f"Ticket created: {ticket.reference_number} with verification code {ticket.verification_code}")
            
            # Create payment intent
            description = f"Ferry ticket: {route}"
            payment_result = create_payment_intent(amount, description, user.email, user.name)
            
            if 'error' in payment_result:
                logger.error(f"Payment intent failed: {payment_result['error']}")
                flash('Unable to process payment. Please try again.', 'error')
                return render_template('payment_failed.html', ticket=ticket, error='Payment failed')
            
            payment_intent_id = payment_result['data']['id']
            ticket.payment_intent_id = payment_intent_id
            db.session.commit()
            
            # Create checkout session
            success_url = url_for('payment_success', ticket_id=ticket.id, _external=True)
            cancel_url = url_for('payment_cancel', ticket_id=ticket.id, _external=True)
            
            checkout_result = create_checkout_session(payment_intent_id, success_url, cancel_url)
            
            if 'error' in checkout_result:
                logger.error(f"Checkout failed: {checkout_result['error']}")
                flash('Unable to create checkout session. Please try again.', 'error')
                return render_template('payment_failed.html', ticket=ticket, error='Checkout failed')
            
            checkout_url = checkout_result['data']['attributes']['checkout_url']
            checkout_id = checkout_result['data']['id']
            
            ticket.checkout_session_id = checkout_id
            ticket.checkout_url = checkout_url
            db.session.commit()
            
            return redirect(checkout_url)
            
        except Exception as e:
            logger.exception("Booking error")
            db.session.rollback()
            flash(f'Booking failed: {str(e)}', 'error')
            return redirect(url_for('book'))
    
    return render_template('book.html', 
                         paymongo_public_key=PAYMONGO_PUBLIC_KEY, 
                         routes=active_routes,
                         default_one_way=default_one_way,
                         default_round_trip=default_round_trip)

@app.route('/payment-success/<int:ticket_id>')
def payment_success(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    
    if USE_MOCK_PAYMENTS:
        ticket.payment_status = 'paid'
        ticket.payment_method = 'Mock Payment'
        db.session.commit()
        flash('Payment successful! Your ticket is confirmed.', 'success')
        return render_template('payment_success.html', ticket=ticket)
    
    if ticket.payment_intent_id:
        payment_data = get_payment_intent(ticket.payment_intent_id)
        
        if payment_data:
            status = payment_data['data']['attributes']['status']
            
            if status == 'succeeded':
                ticket.payment_status = 'paid'
                db.session.commit()
                flash('Payment successful! Your ticket is confirmed.', 'success')
                return render_template('payment_success.html', ticket=ticket)
    
    ticket.payment_status = 'processing'
    db.session.commit()
    flash('Payment is being processed.', 'info')
    return render_template('payment_processing.html', ticket=ticket)

@app.route('/payment-cancel/<int:ticket_id>')
def payment_cancel(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    ticket.payment_status = 'cancelled'
    db.session.commit()
    flash('Payment was cancelled. You can try again.', 'info')
    return render_template('payment_failed.html', ticket=ticket, error='Payment was cancelled')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('home'))

@app.route('/tickets')
def tickets():
    if 'user_id' not in session:
        flash('Please login to view your tickets.', 'error')
        return redirect(url_for('login'))
    
    user_tickets = Ticket.query.filter_by(passenger_email=session['user_email']).order_by(Ticket.created_at.desc()).all()
    return render_template('tickets.html', tickets=user_tickets)

@app.route('/ticket/<int:ticket_id>')
def ticket_detail(ticket_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    ticket = Ticket.query.get_or_404(ticket_id)
    
    if ticket.passenger_email != session['user_email'] and not session.get('is_admin'):
        flash('You do not have permission to view this ticket.', 'error')
        return redirect(url_for('tickets'))
    
    return render_template('ticket_detail.html', ticket=ticket)

@app.route('/retry-payment/<int:ticket_id>')
def retry_payment(ticket_id):
    if 'user_id' not in session:
        flash('Please login to continue.', 'error')
        return redirect(url_for('login'))
    
    ticket = Ticket.query.get_or_404(ticket_id)
    
    if ticket.passenger_email != session['user_email']:
        flash('You do not have permission to pay for this ticket.', 'error')
        return redirect(url_for('tickets'))
    
    if ticket.payment_status == 'paid':
        flash('This ticket is already paid.', 'info')
        return redirect(url_for('ticket_detail', ticket_id=ticket.id))
    
    try:
        description = f"Ferry ticket: {ticket.route}"
        payment_result = create_payment_intent(ticket.amount, description, session['user_email'], session['user_name'])
        
        if 'error' in payment_result:
            logger.error(f"Payment intent failed: {payment_result['error']}")
            flash('Unable to process payment. Please try again.', 'error')
            return redirect(url_for('ticket_detail', ticket_id=ticket.id))
        
        payment_intent_id = payment_result['data']['id']
        ticket.payment_intent_id = payment_intent_id
        ticket.payment_status = 'pending'
        db.session.commit()
        
        success_url = url_for('payment_success', ticket_id=ticket.id, _external=True)
        cancel_url = url_for('payment_cancel', ticket_id=ticket.id, _external=True)
        
        checkout_result = create_checkout_session(payment_intent_id, success_url, cancel_url)
        
        if 'error' in checkout_result:
            logger.error(f"Checkout failed: {checkout_result['error']}")
            flash('Unable to create checkout session. Please try again.', 'error')
            return redirect(url_for('ticket_detail', ticket_id=ticket.id))
        
        checkout_url = checkout_result['data']['attributes']['checkout_url']
        checkout_id = checkout_result['data']['id']
        
        ticket.checkout_session_id = checkout_id
        ticket.checkout_url = checkout_url
        db.session.commit()
        
        return redirect(checkout_url)
        
    except Exception as e:
        logger.exception(f"Retry payment error for ticket {ticket_id}")
        flash(f'An error occurred: {str(e)}', 'error')
        return redirect(url_for('ticket_detail', ticket_id=ticket.id))

# ====================
# User Profile Routes
# ====================
@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash('Please login to view your profile.', 'error')
        return redirect(url_for('login'))
    
    user = User.query.get_or_404(session['user_id'])
    user_tickets = Ticket.query.filter_by(user_id=user.id).order_by(Ticket.created_at.desc()).limit(5).all()
    
    return render_template('profile.html', user=user, tickets=user_tickets)

@app.route('/profile/edit', methods=['GET', 'POST'])
def profile_edit():
    if 'user_id' not in session:
        flash('Please login to edit your profile.', 'error')
        return redirect(url_for('login'))
    
    user = User.query.get_or_404(session['user_id'])
    
    if request.method == 'POST':
        user.name = request.form.get('name', user.name)
        user.phone = request.form.get('phone', user.phone)
        user.address = request.form.get('address', user.address)
        
        db.session.commit()
        
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('profile'))
    
    return render_template('profile_edit.html', user=user)

@app.route('/profile/change-password', methods=['GET', 'POST'])
def change_password():
    if 'user_id' not in session:
        flash('Please login to change your password.', 'error')
        return redirect(url_for('login'))
    
    user = User.query.get_or_404(session['user_id'])
    
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if user.password != current_password:
            flash('Current password is incorrect.', 'error')
            return render_template('change_password.html')
        
        if len(new_password) < 6:
            flash('New password must be at least 6 characters long.', 'error')
            return render_template('change_password.html')
        
        if new_password != confirm_password:
            flash('New passwords do not match.', 'error')
            return render_template('change_password.html')
        
        user.password = new_password
        db.session.commit()
        
        flash('Password changed successfully!', 'success')
        return redirect(url_for('profile'))
    
    return render_template('change_password.html')

@app.route('/profile/delete-account', methods=['POST'])
def delete_account():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user = User.query.get_or_404(session['user_id'])
    
    if user.is_admin:
        flash('Admins cannot delete their accounts.', 'error')
        return redirect(url_for('profile'))
    
    Ticket.query.filter_by(user_id=user.id).delete()
    session.clear()
    db.session.delete(user)
    db.session.commit()
    
    flash('Your account has been deleted.', 'info')
    return redirect(url_for('home'))

# ====================
# Kiosk Routes
# ====================
@app.route('/kiosk', methods=['GET', 'POST'])
def kiosk():
    active_routes = FerryRoute.query.filter_by(is_active=True).all()
    
    if request.method == 'POST':
        try:
            from_location = request.form.get('from')
            to_location = request.form.get('to')
            departure_date = request.form.get('departure_date')
            return_date = request.form.get('return_date')
            trip_type = request.form.get('trip_type', 'one-way')
            passenger_name = request.form.get('passenger_name', '').strip()
            passenger_email = request.form.get('passenger_email', '').strip().lower()
            passenger_phone = request.form.get('passenger_phone', '').strip()
            special_requests = request.form.get('special_requests', '')
            departure_schedule_id = request.form.get('departure_schedule')
            return_schedule_id = request.form.get('return_schedule')
            departure_time = request.form.get('departure_time')
            return_time = request.form.get('return_time')
            
            if not all([from_location, to_location, departure_date, passenger_name, passenger_email, passenger_phone]):
                flash('Please fill in all required fields', 'error')
                return redirect(url_for('kiosk'))
            
            if from_location == to_location:
                flash('Departure and destination cannot be the same', 'error')
                return redirect(url_for('kiosk'))
            
            if '@' not in passenger_email or '.' not in passenger_email:
                flash('Please enter a valid email address', 'error')
                return redirect(url_for('kiosk'))
            
            route_config = FerryRoute.query.filter_by(origin=from_location, destination=to_location, is_active=True).first()
            
            if not route_config:
                flash(f'The route from {from_location} to {to_location} is not available.', 'error')
                return redirect(url_for('kiosk'))
            
            if trip_type == 'one-way':
                amount = route_config.one_way_price
            else:
                amount = route_config.round_trip_price
            
            if departure_schedule_id:
                departure_schedule = FerrySchedule.query.get(departure_schedule_id)
                if departure_schedule:
                    departure_time = departure_schedule.departure_time
            
            if not departure_time:
                departure_time = "08:00 AM"
            
            if trip_type == 'round-trip':
                if return_schedule_id:
                    return_schedule = FerrySchedule.query.get(return_schedule_id)
                    if return_schedule:
                        return_time = return_schedule.departure_time
                if not return_time:
                    return_time = "06:00 PM"
            
            if trip_type == 'round-trip' and return_date:
                route = f"{from_location} ↔ {to_location} (Round Trip)"
            else:
                route = f"{from_location} → {to_location}"
            
            user = User.query.filter_by(email=passenger_email).first()
            
            if not user:
                user = User(
                    name=passenger_name,
                    email=passenger_email,
                    password='kiosk-' + str(uuid.uuid4().hex[:8]),
                    phone=passenger_phone,
                    is_admin=False,
                    is_active=True
                )
                db.session.add(user)
                db.session.commit()
                logger.info(f"New user created from kiosk: {passenger_email}")
            
            ticket = Ticket(
                user_id=user.id,
                passenger_name=passenger_name,
                passenger_email=passenger_email,
                passenger_phone=passenger_phone,
                route=route,
                departure_date=departure_date,
                departure_time=departure_time,
                return_date=return_date if trip_type == 'round-trip' else None,
                return_time=return_time if trip_type == 'round-trip' else None,
                trip_type=trip_type,
                amount=amount,
                payment_status='pending',
                special_requests=special_requests,
                payment_method='Kiosk Payment'
            )
            db.session.add(ticket)
            db.session.commit()
            
            ticket.reference_number = f"KSK{datetime.now().strftime('%Y%m%d')}{ticket.id:04d}"
            unique_string = f"{ticket.reference_number}{ticket.id}{ticket.passenger_email}{ticket.created_at}"
            hash_object = hashlib.md5(unique_string.encode())
            ticket.verification_code = f"SHIP{int(hash_object.hexdigest()[:8], 16) % 10000000:07d}"
            db.session.commit()
            
            logger.info(f"Kiosk ticket created: {ticket.reference_number} for {passenger_email}")
            
            description = f"Kiosk booking: {route}"
            payment_result = create_mock_payment_intent(amount, description, passenger_email, passenger_name)
            
            if 'error' in payment_result:
                logger.error(f"Kiosk payment intent failed: {payment_result['error']}")
                flash('Unable to process payment. Please try again.', 'error')
                return render_template('kiosk.html', routes=active_routes)
            
            payment_intent_id = payment_result['data']['id']
            ticket.payment_intent_id = payment_intent_id
            db.session.commit()
            
            success_url = url_for('kiosk_payment_success', ticket_id=ticket.id, _external=True)
            cancel_url = url_for('kiosk_payment_cancel', ticket_id=ticket.id, _external=True)
            
            checkout_result = create_mock_checkout_session(payment_intent_id, success_url, cancel_url)
            
            if 'error' in checkout_result:
                logger.error(f"Kiosk checkout failed: {checkout_result['error']}")
                flash('Unable to create checkout session. Please try again.', 'error')
                return redirect(url_for('kiosk'))
            
            checkout_url = checkout_result['data']['attributes']['checkout_url']
            checkout_id = checkout_result['data']['id']
            
            ticket.checkout_session_id = checkout_id
            ticket.checkout_url = checkout_url
            db.session.commit()
            
            return redirect(checkout_url)
            
        except Exception as e:
            logger.exception("Kiosk booking error")
            db.session.rollback()
            flash(f'Booking failed: {str(e)}', 'error')
            return redirect(url_for('kiosk'))
    
    return render_template('kiosk.html', routes=active_routes)

@app.route('/kiosk-payment-success/<int:ticket_id>')
def kiosk_payment_success(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    
    ticket.payment_status = 'paid'
    ticket.payment_method = 'Kiosk Payment'
    db.session.commit()
    
    flash('Payment successful! Your ticket has been confirmed.', 'success')
    
    return render_template('kiosk_payment_success.html', ticket=ticket)

@app.route('/kiosk-payment-cancel/<int:ticket_id>')
def kiosk_payment_cancel(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    ticket.payment_status = 'cancelled'
    db.session.commit()
    
    flash('Payment was cancelled. Please try again.', 'error')
    return redirect(url_for('kiosk'))

@app.route('/kiosk/receipt/<int:ticket_id>')
def kiosk_receipt(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    
    if ticket.payment_status != 'paid':
        flash('Receipt is only available for paid tickets.', 'error')
        return redirect(url_for('kiosk'))
    
    try:
        pdf_buffer = generate_ticket_receipt(ticket)
        db.session.commit()
        
        response = make_response(pdf_buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename=Kiosk_Receipt_{ticket.reference_number}.pdf'
        
        return response
        
    except Exception as e:
        logger.error(f"Kiosk receipt generation error: {str(e)}")
        flash('Error generating receipt. Please try again.', 'error')
        return redirect(url_for('kiosk'))

# ====================
# Admin Routes
# ====================
@app.route('/admin')
@admin_required
def admin_dashboard():
    total_users = User.query.count()
    total_tickets = Ticket.query.count()
    total_revenue = db.session.query(db.func.sum(Ticket.amount)).filter_by(payment_status='paid').scalar() or 0
    pending_tickets = Ticket.query.filter_by(payment_status='pending').count()
    total_routes = FerryRoute.query.count()
    total_schedules = FerrySchedule.query.count()
    
    recent_users = User.query.order_by(User.created_at.desc()).limit(5).all()
    recent_tickets = Ticket.query.order_by(Ticket.created_at.desc()).limit(5).all()
    
    return render_template('admin/dashboard.html', 
                         total_users=total_users,
                         total_tickets=total_tickets,
                         total_revenue=total_revenue,
                         pending_tickets=pending_tickets,
                         total_routes=total_routes,
                         total_schedules=total_schedules,
                         recent_users=recent_users,
                         recent_tickets=recent_tickets)

@app.route('/admin/users')
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users)

@app.route('/admin/user/<int:user_id>')
@admin_required
def admin_user_detail(user_id):
    user = User.query.get_or_404(user_id)
    user_tickets = Ticket.query.filter_by(user_id=user_id).order_by(Ticket.created_at.desc()).all()
    return render_template('admin/user_detail.html', user=user, tickets=user_tickets)

@app.route('/admin/user/<int:user_id>/toggle-ban', methods=['POST'])
@admin_required
def admin_toggle_user_ban(user_id):
    user = User.query.get_or_404(user_id)
    
    if user.id == session['user_id']:
        flash('You cannot ban your own account.', 'error')
        return redirect(url_for('admin_users'))
    
    user.is_active = not user.is_active
    db.session.commit()
    
    status = "banned" if not user.is_active else "unbanned"
    flash(f'User {user.email} has been {status}.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    
    if user.id == session['user_id']:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('admin_users'))
    
    Ticket.query.filter_by(user_id=user_id).delete()
    db.session.delete(user)
    db.session.commit()
    
    flash(f'User {user.email} has been deleted.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/tickets')
@admin_required
def admin_tickets():
    tickets = Ticket.query.order_by(Ticket.created_at.desc()).all()
    return render_template('admin/tickets.html', tickets=tickets)

@app.route('/admin/ticket/<int:ticket_id>')
@admin_required
def admin_ticket_detail(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    return render_template('admin/ticket_detail.html', ticket=ticket)

@app.route('/admin/ticket/<int:ticket_id>/update', methods=['POST'])
@admin_required
def admin_update_ticket(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    
    ticket.payment_status = request.form.get('payment_status', ticket.payment_status)
    ticket.status = request.form.get('status', ticket.status)
    ticket.departure_date = request.form.get('departure_date', ticket.departure_date)
    ticket.departure_time = request.form.get('departure_time', ticket.departure_time)
    ticket.return_date = request.form.get('return_date', ticket.return_date)
    ticket.return_time = request.form.get('return_time', ticket.return_time)
    
    amount = request.form.get('amount')
    if amount:
        ticket.amount = float(amount)
    
    ticket.seat_number = request.form.get('seat_number', ticket.seat_number)
    
    db.session.commit()
    
    flash(f'Ticket {ticket.reference_number} has been updated.', 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket.id))

@app.route('/admin/ticket/<int:ticket_id>/delete', methods=['POST'])
@admin_required
def admin_delete_ticket(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    ref = ticket.reference_number
    
    db.session.delete(ticket)
    db.session.commit()
    
    flash(f'Ticket {ref} has been deleted.', 'success')
    return redirect(url_for('admin_tickets'))

@app.route('/admin/make-admin/<int:user_id>', methods=['POST'])
@admin_required
def admin_make_admin(user_id):
    user = User.query.get_or_404(user_id)
    user.is_admin = True
    db.session.commit()
    
    flash(f'{user.email} is now an admin.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/remove-admin/<int:user_id>', methods=['POST'])
@admin_required
def admin_remove_admin(user_id):
    user = User.query.get_or_404(user_id)
    
    if user.id == session['user_id']:
        flash('You cannot remove your own admin privileges.', 'error')
        return redirect(url_for('admin_users'))
    
    user.is_admin = False
    db.session.commit()
    
    flash(f'Admin privileges removed from {user.email}.', 'success')
    return redirect(url_for('admin_users'))

# ====================
# Admin Route Management
# ====================

@app.route('/admin/routes')
@admin_required
def admin_routes():
    routes = FerryRoute.query.order_by(FerryRoute.origin, FerryRoute.destination).all()
    return render_template('admin/routes.html', routes=routes)

@app.route('/admin/routes/add', methods=['GET', 'POST'])
@admin_required
def admin_add_route():
    if request.method == 'POST':
        origin = request.form.get('origin', '').strip()
        destination = request.form.get('destination', '').strip()
        one_way_price = float(request.form.get('one_way_price', 500))
        round_trip_price = float(request.form.get('round_trip_price', 900))
        
        if not origin or not destination:
            flash('Origin and destination are required.', 'error')
            return redirect(url_for('admin_add_route'))
        
        if origin == destination:
            flash('Origin and destination cannot be the same.', 'error')
            return redirect(url_for('admin_add_route'))
        
        existing = FerryRoute.query.filter_by(origin=origin, destination=destination).first()
        if existing:
            flash('This route already exists.', 'error')
            return redirect(url_for('admin_routes'))
        
        route = FerryRoute(
            origin=origin,
            destination=destination,
            one_way_price=one_way_price,
            round_trip_price=round_trip_price
        )
        db.session.add(route)
        db.session.commit()
        
        flash(f'Route {origin} → {destination} added successfully.', 'success')
        return redirect(url_for('admin_routes'))
    
    return render_template('admin/route_form.html', title='Add Route', route=None)

@app.route('/admin/routes/edit/<int:route_id>', methods=['GET', 'POST'])
@admin_required
def admin_edit_route(route_id):
    route = FerryRoute.query.get_or_404(route_id)
    
    if request.method == 'POST':
        route.origin = request.form.get('origin', '').strip()
        route.destination = request.form.get('destination', '').strip()
        route.one_way_price = float(request.form.get('one_way_price', 500))
        route.round_trip_price = float(request.form.get('round_trip_price', 900))
        route.is_active = request.form.get('is_active') == 'on'
        
        db.session.commit()
        
        flash(f'Route {route.origin} → {route.destination} updated successfully.', 'success')
        return redirect(url_for('admin_routes'))
    
    return render_template('admin/route_form.html', title='Edit Route', route=route)

@app.route('/admin/routes/delete/<int:route_id>', methods=['POST'])
@admin_required
def admin_delete_route(route_id):
    route = FerryRoute.query.get_or_404(route_id)
    route_name = f"{route.origin} → {route.destination}"
    
    FerrySchedule.query.filter_by(route_id=route_id).delete()
    DisabledDate.query.filter_by(route_id=route_id).delete()
    db.session.delete(route)
    db.session.commit()
    
    flash(f'Route {route_name} deleted successfully.', 'success')
    return redirect(url_for('admin_routes'))

# ====================
# Admin Schedule Management
# ====================

@app.route('/admin/schedules')
@admin_required
def admin_schedules():
    schedules = FerrySchedule.query.all()
    routes = FerryRoute.query.filter_by(is_active=True).all()
    return render_template('admin/schedules.html', schedules=schedules, routes=routes)

@app.route('/admin/schedules/add', methods=['POST'])
@admin_required
def admin_add_schedule():
    route_id = request.form.get('route_id')
    departure_time = request.form.get('departure_time')
    arrival_time = request.form.get('arrival_time')
    duration = request.form.get('duration')
    vessel_name = request.form.get('vessel_name')
    days_of_week = request.form.get('days_of_week', 'Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday')
    
    if not route_id or not departure_time or not arrival_time:
        flash('Route, departure time, and arrival time are required.', 'error')
        return redirect(url_for('admin_schedules'))
    
    schedule = FerrySchedule(
        route_id=int(route_id),
        departure_time=departure_time,
        arrival_time=arrival_time,
        duration=duration,
        vessel_name=vessel_name,
        days_of_week=days_of_week
    )
    db.session.add(schedule)
    db.session.commit()
    
    flash('Schedule added successfully.', 'success')
    return redirect(url_for('admin_schedules'))

@app.route('/admin/schedules/edit/<int:schedule_id>', methods=['POST'])
@admin_required
def admin_edit_schedule(schedule_id):
    schedule = FerrySchedule.query.get_or_404(schedule_id)
    
    schedule.departure_time = request.form.get('departure_time', schedule.departure_time)
    schedule.arrival_time = request.form.get('arrival_time', schedule.arrival_time)
    schedule.duration = request.form.get('duration', schedule.duration)
    schedule.vessel_name = request.form.get('vessel_name', schedule.vessel_name)
    schedule.days_of_week = request.form.get('days_of_week', schedule.days_of_week)
    schedule.is_active = request.form.get('is_active') == 'on'
    
    db.session.commit()
    
    flash('Schedule updated successfully.', 'success')
    return redirect(url_for('admin_schedules'))

@app.route('/admin/schedules/delete/<int:schedule_id>', methods=['POST'])
@admin_required
def admin_delete_schedule(schedule_id):
    schedule = FerrySchedule.query.get_or_404(schedule_id)
    db.session.delete(schedule)
    db.session.commit()
    
    flash('Schedule deleted successfully.', 'success')
    return redirect(url_for('admin_schedules'))

# ====================
# Admin Disabled Dates Management
# ====================

@app.route('/admin/dates')
@admin_required
def admin_dates():
    routes = FerryRoute.query.filter_by(is_active=True).all()
    disabled_dates = DisabledDate.query.order_by(DisabledDate.date.desc()).all()
    return render_template('admin/dates.html', routes=routes, disabled_dates=disabled_dates)

@app.route('/admin/disabled-dates/add', methods=['POST'])
@admin_required
def admin_add_disabled_date():
    route_id = request.form.get('route_id')
    date = request.form.get('date')
    reason = request.form.get('reason', '')
    
    if not date:
        flash('Date is required.', 'error')
        return redirect(url_for('admin_dates'))
    
    if route_id == 'all':
        route_id = None
    
    existing = DisabledDate.query.filter_by(route_id=route_id, date=date).first()
    if existing:
        flash('This date is already disabled for this route.', 'error')
        return redirect(url_for('admin_dates'))
    
    disabled_date = DisabledDate(
        route_id=route_id,
        date=date,
        reason=reason
    )
    db.session.add(disabled_date)
    db.session.commit()
    
    route_name = "ALL ROUTES" if route_id is None else f"{disabled_date.route.origin} → {disabled_date.route.destination}"
    flash(f'Date {date} has been disabled for {route_name}.', 'success')
    return redirect(url_for('admin_dates'))

@app.route('/admin/disabled-dates/remove/<int:date_id>', methods=['POST'])
@admin_required
def admin_remove_disabled_date(date_id):
    disabled_date = DisabledDate.query.get_or_404(date_id)
    date_str = disabled_date.date
    route_name = "ALL ROUTES" if not disabled_date.route_id else f"{disabled_date.route.origin} → {disabled_date.route.destination}"
    
    db.session.delete(disabled_date)
    db.session.commit()
    
    flash(f'Date {date_str} has been enabled for {route_name}.', 'success')
    return redirect(url_for('admin_dates'))

@app.route('/admin/disabled-dates/delete/<int:date_id>', methods=['POST'])
@admin_required
def admin_delete_disabled_date(date_id):
    disabled_date = DisabledDate.query.get_or_404(date_id)
    db.session.delete(disabled_date)
    db.session.commit()
    
    flash('Disabled date entry deleted.', 'success')
    return redirect(url_for('admin_dates'))

# ====================
# API Routes
# ====================

@app.route('/api/schedules')
def api_get_schedules():
    from_location = request.args.get('from')
    to_location = request.args.get('to')
    
    if not from_location or not to_location:
        return jsonify({'schedules': [], 'error': 'Missing parameters'})
    
    route = FerryRoute.query.filter_by(origin=from_location, destination=to_location, is_active=True).first()
    
    if not route:
        return jsonify({'schedules': [], 'error': 'Route not found'})
    
    schedules = FerrySchedule.query.filter_by(route_id=route.id, is_active=True).all()
    
    schedules_list = []
    for schedule in schedules:
        schedules_list.append({
            'id': schedule.id,
            'departure_time': schedule.departure_time,
            'arrival_time': schedule.arrival_time,
            'duration': schedule.duration,
            'vessel_name': schedule.vessel_name,
            'days_of_week': schedule.days_of_week
        })
    
    return jsonify({'schedules': schedules_list, 'route': f"{route.origin} → {route.destination}"})

@app.route('/api/check-date-disabled')
def api_check_date_disabled():
    from_location = request.args.get('from')
    to_location = request.args.get('to')
    date = request.args.get('date')
    
    if not date:
        return jsonify({'disabled': False, 'message': 'No date provided'})
    
    all_routes_disabled = DisabledDate.query.filter_by(route_id=None, date=date).first()
    if all_routes_disabled:
        reason = all_routes_disabled.reason or 'No trips available on this date'
        return jsonify({
            'disabled': True, 
            'reason': reason,
            'message': f'❌ This date is disabled for all routes. Reason: {reason}'
        })
    
    if from_location and to_location:
        route = FerryRoute.query.filter_by(origin=from_location, destination=to_location).first()
        if route:
            route_disabled = DisabledDate.query.filter_by(route_id=route.id, date=date).first()
            if route_disabled:
                reason = route_disabled.reason or f'No trips available for {from_location} → {to_location}'
                return jsonify({
                    'disabled': True, 
                    'reason': reason,
                    'message': f'❌ This date is disabled for {from_location} → {to_location}. Reason: {reason}'
                })
    
    return jsonify({'disabled': False, 'message': 'Date is available'})

@app.route('/api/route-price')
def api_get_route_price():
    """Get route prices for kiosk"""
    from_location = request.args.get('from')
    to_location = request.args.get('to')
    
    if not from_location or not to_location:
        return jsonify({'success': False, 'error': 'Missing parameters'})
    
    route = FerryRoute.query.filter_by(origin=from_location, destination=to_location, is_active=True).first()
    
    if not route:
        route = FerryRoute.query.filter_by(origin=from_location, destination=to_location).first()
    
    if not route:
        return jsonify({'success': False, 'error': 'Route not found'})
    
    return jsonify({
        'success': True,
        'route_id': route.id,
        'one_way_price': route.one_way_price,
        'round_trip_price': route.round_trip_price,
        'origin': route.origin,
        'destination': route.destination
    })

# ====================
# Utility Routes
# ====================
@app.route('/create-test-user')
def create_test_user():
    try:
        test_email = 'test@example.com'
        existing = User.query.filter_by(email=test_email).first()
        
        if existing:
            return jsonify({
                'message': 'Test user already exists',
                'email': test_email,
                'password': 'password123',
                'is_admin': existing.is_admin
            })
        
        is_first_user = User.query.count() == 0
        
        test_user = User(
            name='Test User',
            email=test_email,
            password='password123',
            phone='1234567890',
            is_admin=is_first_user
        )
        db.session.add(test_user)
        db.session.commit()
        
        return jsonify({
            'message': 'Test user created successfully',
            'email': test_email,
            'password': 'password123',
            'is_admin': is_first_user
        })
        
    except Exception as e:
        logger.error(f"Error creating test user: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/create-admin')
def create_admin_user():
    try:
        admin_email = 'admin@shippydash.com'
        existing = User.query.filter_by(email=admin_email).first()
        
        if existing:
            if not existing.is_admin:
                existing.is_admin = True
                db.session.commit()
                return jsonify({
                    'message': 'Existing user promoted to admin',
                    'email': admin_email,
                    'password': 'admin123'
                })
            return jsonify({
                'message': 'Admin user already exists',
                'email': admin_email,
                'password': 'admin123'
            })
        
        admin_user = User(
            name='Administrator',
            email=admin_email,
            password='admin123',
            phone='1234567890',
            is_admin=True,
            is_active=True
        )
        db.session.add(admin_user)
        db.session.commit()
        
        return jsonify({
            'message': 'Admin user created successfully',
            'email': admin_email,
            'password': 'admin123'
        })
        
    except Exception as e:
        logger.error(f"Error creating admin user: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/check-db')
def check_db():
    users = User.query.all()
    tickets = Ticket.query.all()
    routes = FerryRoute.query.all()
    
    return jsonify({
        'users': [{'id': u.id, 'name': u.name, 'email': u.email, 'is_admin': u.is_admin} for u in users],
        'tickets': [{'id': t.id, 'ref': t.reference_number, 'amount': t.amount} for t in tickets],
        'routes': [{'id': r.id, 'origin': r.origin, 'destination': r.destination} for r in routes],
        'db_path': app.config['SQLALCHEMY_DATABASE_URI']
    })

@app.route('/mock-status')
def mock_status():
    return jsonify({
        'mock_payments_enabled': USE_MOCK_PAYMENTS,
        'message': 'Mock payments are ' + ('enabled' if USE_MOCK_PAYMENTS else 'disabled')
    })

# ====================
# Error Handlers
# ====================
@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', message='Page not found'), 404

@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    return render_template('error.html', message='Internal server error'), 500

@app.route('/api/disabled-dates')
def api_get_disabled_dates():
    """Get disabled dates for a specific route with reasons"""
    from_location = request.args.get('from')
    to_location = request.args.get('to')
    
    disabled_dates_info = {}
    
    # Get all disabled dates for all routes
    all_routes_disabled = DisabledDate.query.filter_by(route_id=None).all()
    for dd in all_routes_disabled:
        reason = dd.reason or 'No trips available on this date for all routes'
        disabled_dates_info[dd.date] = f"All routes are disabled: {reason}"
    
    # Get disabled dates for specific route if route exists
    if from_location and to_location:
        route = FerryRoute.query.filter_by(origin=from_location, destination=to_location).first()
        if route:
            route_disabled = DisabledDate.query.filter_by(route_id=route.id).all()
            for dd in route_disabled:
                reason = dd.reason or f'No trips available for {from_location} → {to_location} on this date'
                disabled_dates_info[dd.date] = reason
    
    return jsonify({'disabled_dates_info': disabled_dates_info})

# ====================
# Main
# ====================
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        
        # Check and create default routes if none exist
        if FerryRoute.query.count() == 0:
            logger.info("No routes found. Creating default routes...")
            default_routes = [
                FerryRoute(origin='Naval', destination='Cebu', one_way_price=500, round_trip_price=900, is_active=True),
                FerryRoute(origin='Naval', destination='Ormoc', one_way_price=500, round_trip_price=900, is_active=True),
                FerryRoute(origin='Cebu', destination='Naval', one_way_price=500, round_trip_price=900, is_active=True),
                FerryRoute(origin='Cebu', destination='Ormoc', one_way_price=500, round_trip_price=900, is_active=True),
                FerryRoute(origin='Ormoc', destination='Naval', one_way_price=500, round_trip_price=900, is_active=True),
                FerryRoute(origin='Ormoc', destination='Cebu', one_way_price=500, round_trip_price=900, is_active=True),
            ]
            for route in default_routes:
                db.session.add(route)
            db.session.commit()
            logger.info(f"Created {len(default_routes)} default routes")
        else:
            logger.info(f"Found {FerryRoute.query.count()} existing routes")
        
        # Check and create default schedules if none exist
        if FerrySchedule.query.count() == 0:
            logger.info("No schedules found. Creating default schedules...")
            routes = FerryRoute.query.all()
            default_schedule_times = ["08:00 AM", "10:00 AM", "12:00 PM", "02:00 PM", "04:00 PM", "06:00 PM", "08:00 PM"]
            
            for route in routes:
                for i, dep_time in enumerate(default_schedule_times):
                    hour = int(dep_time.split(':')[0])
                    ampm = dep_time.split(' ')[1]
                    arrival_hour = hour + 2
                    if arrival_hour > 12:
                        arrival_hour = arrival_hour - 12
                    arrival_time = f"{arrival_hour:02d}:00 {ampm}"
                    
                    schedule = FerrySchedule(
                        route_id=route.id,
                        departure_time=dep_time,
                        arrival_time=arrival_time,
                        duration="2 hours",
                        vessel_name=f"MV Shippy {i+1}",
                        is_active=True
                    )
                    db.session.add(schedule)
            db.session.commit()
            logger.info(f"Created default schedules for {len(routes)} routes")
        else:
            logger.info(f"Found {FerrySchedule.query.count()} existing schedules")
        
        db_path = os.path.join(app.instance_path, 'tickets.db')
        if os.path.exists(db_path):
            logger.info(f"Database found at: {db_path}")
        else:
            logger.info(f"New database will be created at: {db_path}")
        
        user_count = User.query.count()
        logger.info(f"Existing users in database: {user_count}")
        logger.info(f"Mock payments: {'ENABLED' if USE_MOCK_PAYMENTS else 'DISABLED'}")
        logger.info("Starting ShippyDash application...")
    
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")