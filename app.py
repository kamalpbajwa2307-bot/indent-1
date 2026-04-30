import firebase_admin  # <-- ADDED THIS
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter  # <-- ADDED THIS
from google.api_core.exceptions import ResourceExhausted  # <-- ADDED THIS
import firebase_admin

from firebase_admin import credentials, firestore
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, send_file
from datetime import datetime, timedelta, timezone
import pandas as pd
import io
import os
import math
import json
import base64

# ==========================================
# 1. CONFIGURATION
# ==========================================
app = Flask(__name__)
app.secret_key = 'secure_key_v38_pending_filter_sort'
app.debug = True
# --- AUTO LOGOUT CONFIGURATION ---
app.permanent_session_lifetime = timedelta(minutes=15)

# --- FIREBASE SETUP START ---
firebase_creds_json = os.getenv('FIREBASE_CONFIG')

db = None
if firebase_creds_json:
    try:
        cred_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(cred_dict)
        
        # FIX: Prevent Vercel from crashing on "warm starts" by checking if already initialized
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
            
        db = firestore.client()
        print("Firebase successfully initialized!")
    except Exception as e:
        print(f"Error parsing JSON or initializing Firebase: {e}")
else:
    print("CRITICAL ERROR: FIREBASE_CONFIG environment variable not found!")
# ==========================================
# 2. LOGIC & HELPERS
# ==========================================

class FirestoreEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return {'__type__': 'datetime', 'value': obj.isoformat()}
        return super(FirestoreEncoder, self).default(obj)

def firestore_decoder(dct):
    if '__type__' in dct and dct['__type__'] == 'datetime':
        return datetime.fromisoformat(dct['value'])
    return dct

def get_fy_string(dt):
    if not dt: return ""
    try:
        if isinstance(dt, str):
            dt = datetime.strptime(dt[:10], '%Y-%m-%d')
        start_year = dt.year if dt.month >= 4 else dt.year - 1
        return f"{start_year}-{str(start_year + 1)[-2:]}"
    except: 
        return ""

def format_date_custom(value):
    if not value: return ""
    try: return datetime.strptime(value, '%Y-%m-%d').strftime('%d-%m-%y')
    except: return value 

def format_datetime_custom(value):
    if not value: return ""
    try:
        if isinstance(value, str):
            value = datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f')
        ist_time = value + timedelta(hours=5, minutes=30)
        return ist_time.strftime('%d-%m-%y %I:%M %p')
    except: return ""

app.jinja_env.filters['date_fmt'] = format_date_custom
app.jinja_env.filters['datetime_fmt'] = format_datetime_custom
app.jinja_env.filters['fy_fmt'] = get_fy_string

def get_default_permissions(role):
    p = {
        'indent': {'view': False, 'create': False, 'edit': False, 'delete': False, 'approve': False, 'mark_received': False, 'mark_purchased': False},
        'payment': {'view': False, 'create': False, 'edit': False, 'delete': False, 'approve': False},
        'gatepass': {'view': False, 'create': False, 'edit': False, 'delete': False, 'approve': False},
        'settings': {'view': False}
    }
    if role in ['SuperAdmin', 'Admin']:
        for m in ['indent', 'payment', 'gatepass']:
            p[m] = {'view': True, 'create': True, 'edit': True, 'delete': True, 'approve': True}
        p['indent']['mark_received'] = True
        p['indent']['mark_purchased'] = True
        p['settings']['view'] = True
    elif role == 'Editor':
        for m in ['indent', 'payment', 'gatepass']:
            p[m] = {'view': True, 'create': True, 'edit': True, 'delete': False, 'approve': False}
        p['indent']['mark_received'] = True
        p['indent']['mark_purchased'] = False
    elif role == 'Viewer':
        for m in ['indent', 'payment', 'gatepass']:
            p[m] = {'view': True, 'create': False, 'edit': False, 'delete': False, 'approve': False}
        p['indent']['mark_received'] = True
        p['indent']['mark_purchased'] = False
    return p

def initialize_defaults():
    # Make sure db actually initialized before trying to query it
    if db is None:
        print("WARNING: Database not initialized. Skipping defaults.")
        return

    try:
        # Using FieldFilter to prevent the positional argument warning
        if not list(db.collection('users').where(filter=FieldFilter('username', '==', 'admin1')).stream()):
            db.collection('users').add({'username': 'admin1', 'password': 'super', 'name': 'Super Administrator', 'role': 'SuperAdmin'})
        
        if not len(list(db.collection('units').limit(1).stream())):
            for u in ['KG', 'LTR', 'PCS', 'MTR', 'BOX']: db.collection('units').add({'name': u})
            
        if not len(list(db.collection('departments').limit(1).stream())):
            for d in ['HR', 'IT', 'ELECTRICAL', 'CTP', 'STORE']: db.collection('departments').add({'name': d})
        
        current_fy = get_fy_string(datetime.now())
        if not list(db.collection('financial_years').where(filter=FieldFilter('name', '==', current_fy)).limit(1).stream()):
            db.collection('financial_years').add({'name': current_fy})

    except ResourceExhausted:
        print("WARNING: Firestore quota exceeded. Skipping default initialization.")
    except Exception as e:
        print(f"WARNING: Could not initialize defaults: {e}")

initialize_defaults()

@app.context_processor
def inject_global_vars():
    if 'user_id' in session:
        fys = [doc.to_dict()['name'] for doc in db.collection('financial_years').order_by('name', direction=firestore.Query.DESCENDING).stream()]
        return dict(available_fys=fys)
    return dict(available_fys=[])

def get_next_serial_number(collection_name, target_fy, count=1):
    counter_id = f"{collection_name}_{target_fy}"
    counter_ref = db.collection('counters').document(counter_id)
    
    doc = counter_ref.get()
    if not doc.exists:
        docs = db.collection(collection_name).where('fy', '==', target_fy).stream()
        max_serial = 0
        for d in docs:
            try:
                val = int(d.to_dict().get('serial_no', 0))
                if val > max_serial: max_serial = val
            except: pass
        counter_ref.set({'last_value': max_serial})

    transaction = db.transaction()
    @firestore.transactional
    def update_in_transaction(transaction, ref):
        snapshot = ref.get(transaction=transaction)
        current_max = snapshot.get('last_value')
        new_max = current_max + count
        transaction.update(ref, {'last_value': new_max})
        return current_max + 1

    return update_in_transaction(transaction, counter_ref)

def delete_last_entry_helper(collection_name, doc_id, target_fy):
    counter_ref = db.collection('counters').document(f"{collection_name}_{target_fy}")
    counter_snap = counter_ref.get()
    
    if not counter_snap.exists: return False
    current_max = counter_snap.get('last_value')
    
    doc_ref = db.collection(collection_name).document(doc_id)
    doc = doc_ref.get()
    if not doc.exists: return False
    
    try:
        doc_serial = int(doc.to_dict().get('serial_no', 0))
        if doc_serial == current_max:
            doc_ref.delete()
            counter_ref.update({'last_value': current_max - 1})
            return True
    except: pass
    return False

def get_units_list():
    return sorted(list(set([doc.to_dict()['name'] for doc in db.collection('units').stream()])))

def get_departments_list():
    return sorted(list(set([doc.to_dict()['name'] for doc in db.collection('departments').stream()])))

def get_people_list():
    return sorted(list(set([doc.to_dict()['name'] for doc in db.collection('indent_persons').stream()])))

def get_companies_list():
    return sorted(list(set([doc.to_dict()['name'] for doc in db.collection('companies').stream()])))

def add_if_new(collection, name):
    if not name or name.lower() == 'other': return
    name = name.strip().upper()
    existing = list(db.collection(collection).where('name', '==', name).stream())
    if not existing:
        db.collection(collection).add({'name': name})

# ==========================================
# 3. HTML TEMPLATES 
# ==========================================

HTML_BASE_HEAD = """
<head>
    <meta charset="UTF-8">
    <title>DPPL System</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.7.2/font/bootstrap-icons.css">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        :root { --primary-green: #2E8B57; --dark-green: #1E5638; --light-green: #E8F5E9; --accent-green: #4CAF50; }
        body { font-family: 'Poppins', sans-serif; background-color: #f8f9fa; }
        .navbar-custom { background: linear-gradient(135deg, var(--dark-green), var(--primary-green)); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .navbar-brand { font-weight: 600; letter-spacing: 0.5px; }
        .navbar-custom .nav-link { color: rgba(255,255,255,0.9) !important; font-weight: 400; transition: all 0.3s; }
        .navbar-custom .nav-link.active { background-color: rgba(255,255,255,0.2) !important; border-radius: 6px; font-weight: 600; }
        .card { border: none; border-radius: 12px; box-shadow: 0 5px 15px rgba(0,0,0,0.05); overflow: hidden; }
        .card-header { background-color: var(--primary-green); color: white; font-weight: 500; }
        .status-received { background-color: #d1e7dd !important; color: #0f5132; }
        .status-cleared { background-color: #e2e3e5 !important; color: #41464b; }
        .text-green { color: var(--primary-green); }
        .small-meta { font-size: 0.75rem; color: #6c757d; line-height: 1.2; display: block; margin-top: 4px; }
        @media print { .no-print { display: none !important; } .card { box-shadow: none !important; border: 1px solid #ddd; } body { background-color: white !important; } }
    </style>
    
    <script>
        function validateFileSize(input) {
            if (input.files && input.files[0]) {
                var fileSizeKB = Math.round(input.files[0].size / 1024);
                if (input.files[0].size > 204800) { 
                    alert("❌ ERROR: Image size is " + fileSizeKB + " KB.\\n\\nMaximum allowed size is 200 KB. Please reduce the image size before uploading.");
                    input.value = "";
                }
            }
        }
        
        function viewImage(dataUri) {
            var existingModal = document.getElementById('dynamicImageModal');
            if (existingModal) {
                existingModal.remove();
            }
            
            var modalHtml = `
            <div class="modal fade" id="dynamicImageModal" tabindex="-1" aria-hidden="true">
              <div class="modal-dialog modal-lg modal-dialog-centered">
                <div class="modal-content border-0 shadow-lg">
                  <div class="modal-header bg-dark text-white border-0 py-2">
                    <h6 class="modal-title mb-0"><i class="bi bi-image me-2"></i>Image Viewer</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                  </div>
                  <div class="modal-body text-center bg-light p-3">
                    <img src="${dataUri}" class="img-fluid rounded" style="max-height: 75vh;">
                  </div>
                </div>
              </div>
            </div>`;
            
            document.body.insertAdjacentHTML('beforeend', modalHtml);
            var myModal = new bootstrap.Modal(document.getElementById('dynamicImageModal'));
            myModal.show();
        }
    </script>
</head>
"""

HTML_NAV = """
<nav class="navbar navbar-expand-lg navbar-dark navbar-custom px-4 py-3 no-print">
    <span class="navbar-brand me-5"><i class="bi bi-tree-fill me-2"></i>DPPL System</span>
    <div class="collapse navbar-collapse">
        <div class="nav nav-pills me-auto">
            {% if session.get('permissions', {}).get('indent', {}).get('view', True) %}
            <a href="{{ url_for('dashboard') }}" class="nav-link {% if system == 'indent' %}active{% endif %}">📦 Indent</a>
            {% endif %}
            
            {% if session.get('permissions', {}).get('payment', {}).get('view', True) %}
            <a href="{{ url_for('payment_dashboard') }}" class="nav-link {% if system == 'payment' %}active{% endif %}">💰 Payment</a>
            {% endif %}
            
            {% if session.get('permissions', {}).get('gatepass', {}).get('view', True) %}
            <a href="{{ url_for('gatepass_dashboard') }}" class="nav-link {% if system == 'gatepass' %}active{% endif %}">🎫 Gate Pass</a>
            {% endif %}
        </div>
        
        <div class="d-flex align-items-center">
            <div class="dropdown me-3">
                <button class="btn btn-sm btn-outline-warning dropdown-toggle fw-bold" type="button" data-bs-toggle="dropdown">
                    FY: {{ session.get('active_fy') }}
                </button>
                <ul class="dropdown-menu dropdown-menu-dark shadow">
                    <li><h6 class="dropdown-header">Select Financial Year</h6></li>
                    {% for fy in available_fys %}
                    <li><a class="dropdown-item {% if fy == session.get('active_fy') %}active bg-success{% endif %}" href="{{ url_for('switch_fy', fy=fy) }}">{{ fy }}</a></li>
                    {% endfor %}
                </ul>
            </div>
            
            <div class="text-light d-flex align-items-center">
                <span class="me-3"><small>User:</small> <strong>{{ session['user_name'] }}</strong> <span class="badge bg-light text-success rounded-pill ms-1">{{ session['role'] }}</span></span>
                {% if system == 'indent' %}<a href="{{ url_for('reports') }}" class="btn btn-sm btn-light text-success fw-bold me-2">Reports</a>
                {% elif system == 'payment' %}<a href="{{ url_for('payment_reports') }}" class="btn btn-sm btn-light text-success fw-bold me-2">Reports</a>
                {% elif system == 'gatepass' %}<a href="{{ url_for('gatepass_reports') }}" class="btn btn-sm btn-light text-success fw-bold me-2">Reports</a>{% endif %}
                
                {% if session.get('role') in ['Admin', 'SuperAdmin'] or session.get('permissions', {}).get('settings', {}).get('view') %}
                <a href="{{ url_for('settings') }}" class="btn btn-sm btn-outline-light me-2"><i class="bi bi-gear-fill"></i> Settings</a>
                {% endif %}
                
                <a href="{{ url_for('logout') }}" class="btn btn-sm btn-danger rounded-pill px-3">Logout</a>
            </div>
        </div>
    </div>
</nav>
"""

HTML_LOGIN = """
<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """
<body class="bg-light d-flex align-items-center justify-content-center" style="height: 100vh; background: linear-gradient(135deg, #e8f5e9 0%, #c8e6c9 100%);">
    <div class="card shadow-lg p-4" style="width: 380px; border-radius: 15px;">
        <div class="text-center mb-4">
            <h2 class="text-green fw-bold"><i class="bi bi-tree-fill"></i> DPPL</h2>
            <h5 class="text-muted">Internal System</h5>
        </div>
        {% with messages = get_flashed_messages() %}
            {% if messages %}<div class="alert alert-danger rounded-3">{{ messages[0] }}</div>{% endif %}
        {% endwith %}
        <form method="POST" action="{{ url_for('login') }}">
            <div class="form-floating mb-3">
                <input type="text" name="username" class="form-control" id="uInput" placeholder="Username" required>
                <label for="uInput">Username</label>
            </div>
            <div class="form-floating mb-3">
                <input type="password" name="password" class="form-control" id="pInput" placeholder="Password" required>
                <label for="pInput">Password</label>
            </div>
            <div class="form-check mb-3">
                <input class="form-check-input" type="checkbox" name="change_password" id="cpCheck">
                <label class="form-check-label small" for="cpCheck">I want to change my password</label>
            </div>
            <button type="submit" class="btn btn-primary w-100 py-2 rounded-3 fw-bold">Login</button>
        </form>
        <div class="text-center mt-3 small text-muted">&copy; 2026 DPPL Internal Systems</div>
    </div>
</body></html>
"""

HTML_CHANGE_PASS = """
<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """
<body class="bg-light d-flex align-items-center justify-content-center" style="height: 100vh;">
    <div class="card shadow p-4" style="width: 400px;">
        <h4 class="text-center mb-3 text-green">Change Password</h4>
        {% with messages = get_flashed_messages() %}
            {% if messages %}<div class="alert alert-info">{{ messages[0] }}</div>{% endif %}
        {% endwith %}
        <form method="POST">
            <div class="mb-2"><label>Username</label><input type="text" name="username" class="form-control" required></div>
            <div class="mb-2"><label>Old Password</label><input type="password" name="old_password" class="form-control" required></div>
            <div class="mb-3"><label>New Password</label><input type="password" name="new_password" class="form-control" required></div>
            <button type="submit" class="btn btn-success w-100">Update Password</button>
            <a href="{{ url_for('login') }}" class="btn btn-link w-100 mt-2 text-decoration-none text-muted">Back to Login</a>
        </form>
    </div>
</body></html>
"""

# ==========================================
# INDENT TEMPLATES
# ==========================================
HTML_DASHBOARD_INDENT = """
<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """
<body>""" + HTML_NAV + """
<div class="container-fluid mt-4 px-4">
    <div class="d-flex justify-content-between align-items-center mb-4 no-print">
        <h3 class="text-green fw-bold">Indent Dashboard 
            <span class="badge bg-secondary ms-2" style="font-size: 0.5em; vertical-align: middle;">FY {{ session.get('active_fy') }}</span>
        </h3>
        <div class="d-flex gap-2 align-items-center">
            <div class="btn-group shadow-sm me-2">
                <a href="{{ url_for('dashboard', status='All', search=request.args.get('search', '')) }}" class="btn btn-sm {{ 'btn-success' if current_status == 'All' else 'btn-outline-success' }}">All Items</a>
                <a href="{{ url_for('dashboard', status='Pending', search=request.args.get('search', '')) }}" class="btn btn-sm {{ 'btn-warning' if current_status == 'Pending' else 'btn-outline-warning' }}"><i class="bi bi-clock-history"></i> Pending</a>
            </div>
            
            <form method="GET" class="d-flex me-2">
                <input type="hidden" name="status" value="{{ current_status }}">
                <div class="input-group" style="width: 250px;">
                    <input type="text" name="search" class="form-control form-control-sm" placeholder="Search Item, Created By, Dept..." value="{{ request.args.get('search', '') }}">
                    <button class="btn btn-primary btn-sm" type="submit"><i class="bi bi-search"></i></button>
                    {% if request.args.get('search') or current_status == 'Pending' %}
                    <a href="{{ url_for('dashboard') }}" class="btn btn-outline-secondary btn-sm">Reset</a>
                    {% endif %}
                </div>
            </form>

            {% if session.get('permissions', {}).get('indent', {}).get('create') %}
                <a href="{{ url_for('create') }}" class="btn btn-success shadow-sm px-4"><i class="bi bi-plus-lg"></i> New Indent</a>
            {% endif %}
        </div>
    </div>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category if category != 'message' else 'info' }} shadow-sm">{{ message }}</div>{% endfor %}{% endif %}
    {% endwith %}
    <div class="card shadow">
        <div class="card-body p-0">
            {% if session.get('permissions', {}).get('indent', {}).get('approve') or session.get('permissions', {}).get('indent', {}).get('mark_received') or session['role'] == 'SuperAdmin' %}
            <form id="bulkForm" method="POST" action="{{ url_for('bulk_update') }}">
                <div class="p-3 bg-light border-bottom no-print">
                    <div class="row g-2 align-items-end">
                        {% if session.get('permissions', {}).get('indent', {}).get('approve') or session['role'] == 'SuperAdmin' %}
                        <div class="col-md-6 border-end pe-3">
                            <label class="small text-muted fw-bold text-uppercase mb-1">Approval Action</label>
                            <div class="input-group">
                                <span class="input-group-text bg-white border-end-0">By:</span>
                                <select name="approver_name" class="form-select border-start-0 ps-0">
                                    {% for u in users %}<option value="{{ u.name }}" {% if u.name == session['user_name'] %}selected{% endif %}>{{ u.name }}</option>{% endfor %}
                                </select>
                                {% if session.get('permissions', {}).get('indent', {}).get('approve') or session['role'] == 'SuperAdmin' %}
                                    <button type="submit" name="action" value="Approved" class="btn btn-success">Approve</button>
                                {% endif %}
                                {% if session['role'] == 'SuperAdmin' %}
                                    <button type="submit" name="action" value="Hold" class="btn btn-secondary text-white">Hold</button>
                                    <button type="submit" name="action" value="Rejected" class="btn btn-danger">Reject</button>
                                {% endif %}
                            </div>
                        </div>
                        {% endif %}
                        
                        {% if session.get('permissions', {}).get('indent', {}).get('mark_received') or session['role'] == 'SuperAdmin' %}
                        <div class="col-md-6 ps-3">
                            <label class="small text-muted fw-bold text-uppercase mb-1">Mark Received</label>
                            <div class="input-group">
                                <input type="date" name="bulk_received_date" class="form-control" value="{{ today }}">
                                <button type="submit" name="action" value="Received" class="btn btn-dark">Mark Selected Received</button>
                            </div>
                        </div>
                        {% endif %}
                    </div>
                </div>
            {% endif %}
            
            <div class="table-responsive">
                <table class="table table-hover align-middle mb-0">
                    <thead>
                        <tr>
                            {% if session.get('permissions', {}).get('indent', {}).get('approve') or session.get('permissions', {}).get('indent', {}).get('mark_received') or session['role'] == 'SuperAdmin' %}
                                <th class="no-print text-center" style="width: 40px;"><input type="checkbox" onclick="toggleAll(this)"></th>
                            {% endif %}
                            <th>S.No</th>
                            <th>Date / Created By</th>
                            <th>Image</th>
                            <th>Dept / Person</th>
                            <th>Item Details</th>
                            <th>Qty</th>
                            <th>Assigned</th>
                            <th>Approved By</th>
                            <th>Status</th>
                            <th>Received</th>
                            <th class="no-print">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for indent in indents %}
                        <tr class="{% if indent.received_status == 'Received' %}status-received{% endif %}">
                            {% if session.get('permissions', {}).get('indent', {}).get('approve') or session.get('permissions', {}).get('indent', {}).get('mark_received') or session['role'] == 'SuperAdmin' %}
                            <td class="no-print text-center"><input type="checkbox" name="selected_ids[]" value="{{ indent.id }}" class="row-checkbox" form="bulkForm"></td>
                            {% endif %}
                            <td class="fw-bold text-secondary">{{ indent.fy }}/{{ indent.serial_no }}</td>
                            <td><span class="fw-bold text-dark">{{ indent.indent_date | date_fmt }}</span><span class="small-meta text-muted">Cr: {{ indent.created_by }}</span></td>
                            <td>
                                {% if indent.image_url and indent.image_url != "" %}
                                    <button type="button" class="btn btn-sm btn-outline-info rounded-pill py-0 px-2 shadow-sm" style="font-size: 0.8rem;" onclick="viewImage('{{ indent.image_url }}')"><i class="bi bi-image"></i> View</button>
                                {% else %}<span class="text-muted small">No Image</span>{% endif %}
                            </td>
                            <td><span class="d-block fw-500">{{ indent.department }}</span><span class="small text-muted">{{ indent.indent_person }}</span></td>
                            <td>
                                <strong class="text-green">{{ indent.item }}</strong>
                                <div class="small text-muted fst-italic">{{ indent.reason }}</div>
                                {% if indent.remarks %}
                                    <div class="small text-secondary mt-1"><span class="fw-bold text-dark">Remarks:</span> {{ indent.remarks }}</div>
                                {% endif %}
                            </td>
                            <td class="fw-bold">{{ indent.quantity }} <span class="text-muted fw-normal">{{ indent.unit }}</span></td>
                            <td class="text-primary small fw-bold">{{ indent.assigned_to }}</td>
                            <td class="small fw-bold">
                                {% if indent.approval_status == 'Approved' %}<span class="text-success">{{ indent.approved_by_name if indent.approved_by_name else '-' }}</span>
                                {% elif indent.approval_status == 'Hold' %}<span class="text-secondary">{{ indent.approved_by_name if indent.approved_by_name else '-' }}</span>
                                {% elif indent.approval_status == 'Rejected' %}<span class="text-danger">{{ indent.approved_by_name if indent.approved_by_name else '-' }}</span>
                                {% else %}-{% endif %}
                            </td>
                            <td>
                                <span class="badge rounded-pill {% if indent.approval_status == 'Approved' %}bg-success{% elif indent.approval_status == 'Rejected' %}bg-danger{% elif indent.approval_status == 'Hold' %}bg-secondary{% else %}bg-warning text-dark{% endif %} mb-1 d-block">{{ indent.approval_status }}</span>
                                {% if indent.purchase_status == 'Purchased' %}<span class="badge rounded-pill bg-info text-dark d-block shadow-sm"><i class="bi bi-cart-check-fill"></i> Purchased</span>{% endif %}
                            </td>
                            <td>
                                {% if indent.received_status == 'Received' %}<span class="badge bg-primary">Received</span><div class="small-meta">{{ indent.received_date | date_fmt }}</div>
                                {% elif indent.received_status == 'Rejected' %}<span class="badge bg-danger">Rejected</span>
                                {% else %}<span class="badge bg-light text-secondary border">Pending</span>{% endif %}
                            </td>
                            <td class="no-print">
                                <div class="btn-group">
                                    {% if session.get('permissions', {}).get('indent', {}).get('edit') %}
                                        <a href="{{ url_for('edit_indent', i_id=indent.id) }}" class="btn btn-sm btn-outline-primary border-0" title="Edit"><i class="bi bi-pencil-square"></i></a>
                                    {% endif %}
                                    
                                    {% if session.get('permissions', {}).get('indent', {}).get('mark_purchased') or session['role'] == 'SuperAdmin' %}
                                        {% if indent.purchase_status != 'Purchased' %}
                                            <a href="{{ url_for('mark_purchased', i_id=indent.id) }}" class="btn btn-sm btn-outline-success border-0" title="Mark Purchased" onclick="return confirm('Mark as Purchased?');"><i class="bi bi-cart-check"></i></a>
                                        {% else %}
                                            <a href="{{ url_for('reset_purchase', i_id=indent.id) }}" class="btn btn-sm btn-outline-warning border-0" title="Reset Purchase" onclick="return confirm('Reset Purchase Status?');"><i class="bi bi-arrow-counterclockwise"></i></a>
                                        {% endif %}
                                    {% endif %}
                                    
                                    {% if session.get('permissions', {}).get('indent', {}).get('delete') %}
                                        <a href="{{ url_for('delete_indent', i_id=indent.id) }}" class="btn btn-sm btn-outline-danger border-0" title="Delete" onclick="return confirm('Delete?')"><i class="bi bi-trash"></i></a>
                                    {% endif %}
                                </div>
                            </td>
                        </tr>
                        {% else %}<tr><td colspan="12" class="text-center py-4 text-muted">No records found for FY {{ session.get('active_fy') }}.</td></tr>{% endfor %}
                    </tbody>
                </table>
            </div>
            {% if session.get('permissions', {}).get('indent', {}).get('approve') or session.get('permissions', {}).get('indent', {}).get('mark_received') or session['role'] == 'SuperAdmin' %}</form>{% endif %}
            
            <div class="d-flex justify-content-between align-items-center p-3 bg-light border-top no-print">
                <div class="small text-muted">Page {{ page }}</div>
                <div>
                    {% if page > 1 %}<a href="{{ url_for('dashboard', page=page-1, status=current_status, search=request.args.get('search', '')) }}" class="btn btn-sm btn-outline-secondary">Previous</a>{% endif %}
                    {% if has_next %}<a href="{{ url_for('dashboard', page=page+1, status=current_status, search=request.args.get('search', '')) }}" class="btn btn-sm btn-outline-secondary">Next</a>{% endif %}
                </div>
            </div>
        </div>
    </div>
</div>
<script>
    function toggleAll(source) {
        checkboxes = document.getElementsByClassName('row-checkbox');
        for(var i=0; i<checkboxes.length; i++) { checkboxes[i].checked = source.checked; }
    }
</script>
</body></html>
"""

HTML_CREATE_MULTI = """
<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body class="bg-light">""" + HTML_NAV + """
<div class="container mt-4">
    <div class="card shadow border-0">
        <div class="card-header bg-success text-white d-flex justify-content-between align-items-center">
            <h4 class="mb-0">Create Indent (FY: {{ session.get('active_fy') }})</h4>
            <span class="badge bg-light text-success">Max Image Size: 200 KB</span>
        </div>
        <div class="card-body bg-white">
            {% with messages = get_flashed_messages(with_categories=true) %}
              {% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }} shadow-sm">{{ message }}</div>{% endfor %}{% endif %}
            {% endwith %}
            
            <div class="alert alert-info py-2 small"><i class="bi bi-info-circle me-1"></i> Entries will be saved strictly under Financial Year <strong>{{ session.get('active_fy') }}</strong>. Dates outside this range will be rejected.</div>
            
            <form method="POST" enctype="multipart/form-data" id="indentForm">
                <div class="row mb-4 p-3 bg-light border rounded-3 mx-0">
                    <div class="col-md-3"><label class="fw-bold small text-uppercase text-muted">Date</label><input type="date" name="indent_date" class="form-control" value="{{ today }}" required></div>
                    <div class="col-md-3"><label class="fw-bold small text-uppercase text-muted">Department</label><select name="department_select" class="form-select" onchange="checkDept(this)" required><option value="" disabled selected>Select Dept</option>{% for d in departments %}<option value="{{ d }}">{{ d }}</option>{% endfor %}<option value="Other">Other (Add New)</option></select><input type="text" name="custom_department" class="form-control mt-2 d-none" placeholder="Enter New Dept Name" id="customDeptInput"></div>
                    <div class="col-md-3"><label class="fw-bold small text-uppercase text-muted">Indent Person</label><input type="text" name="indent_person" list="personList" class="form-control" placeholder="Type name..."><datalist id="personList">{% for p in persons %}<option value="{{ p }}">{% endfor %}</datalist></div>
                    <div class="col-md-3"><label class="fw-bold small text-uppercase text-muted">Assign To</label><select name="assigned_to" class="form-select">{% for user in users %}<option value="{{ user.name }}">{{ user.name }}</option>{% endfor %}</select></div>
                    
                    {% if session['role'] in ['Admin', 'SuperAdmin'] %}
                    <div class="col-md-12 mt-3 pt-3 border-top">
                        <label class="fw-bold small text-uppercase text-danger d-block">Admin Override: Starting Serial No (Optional)</label>
                        <input type="number" name="manual_serial" class="form-control d-inline-block" style="width: 150px;" placeholder="e.g. 1">
                        <small class="text-muted ms-2">Leave blank to auto-continue from the last number.</small>
                    </div>
                    {% endif %}
                </div>
                
                <h5 class="mb-3 text-green border-bottom pb-2">Item Details</h5>
                <div class="table-responsive">
                    <table class="table table-bordered align-middle" id="itemsTable">
                        <thead class="table-light text-center"><tr><th width="20%">Item Name</th><th width="15%">Reason</th><th width="15%">Remarks</th><th width="15%">Product Image</th><th width="10%">Qty</th><th width="20%">Unit</th><th width="5%"></th></tr></thead>
                        <tbody>
                            <tr>
                                <td><input type="text" name="item[]" class="form-control" required placeholder="Item Name"></td>
                                <td><input type="text" name="reason[]" class="form-control" placeholder="Why needed?"></td>
                                <td><input type="text" name="remarks[]" class="form-control" placeholder="Notes"></td>
                                <td><input type="file" name="product_image[]" class="form-control form-control-sm" accept="image/*" onchange="validateFileSize(this)"></td>
                                <td><input type="number" name="quantity[]" class="form-control text-center" required></td>
                                <td>
                                    <select name="unit[]" class="form-select unit-select" onchange="checkUnit(this)">{% for u in unit_list %}<option value="{{ u }}">{{ u }}</option>{% endfor %}<option value="Other">Other</option></select>
                                    <input type="text" name="custom_unit[]" class="form-control mt-1 d-none custom-unit" placeholder="Unit">
                                </td>
                                <td class="text-center"><button type="button" class="btn btn-outline-danger btn-sm rounded-circle" onclick="removeRow(this)"><i class="bi bi-x-lg"></i></button></td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <div class="d-flex justify-content-between mt-3">
                    <button type="button" class="btn btn-outline-primary" onclick="addRow()"><i class="bi bi-plus-circle me-1"></i> Add Another Item</button>
                    <button type="submit" class="btn btn-success px-5 fw-bold shadow-sm" id="submitBtn">Submit Entry</button>
                </div>
            </form>
        </div>
    </div>
</div>
<script>
    function checkDept(selectObj){ var customInput = document.getElementById('customDeptInput'); if(selectObj.value === 'Other'){ customInput.classList.remove('d-none'); customInput.required = true; customInput.focus(); } else { customInput.classList.add('d-none'); customInput.required = false; } }
    function checkUnit(selectObj){ var customInput = selectObj.nextElementSibling; if(selectObj.value === 'Other'){ customInput.classList.remove('d-none'); customInput.required = true; } else { customInput.classList.add('d-none'); customInput.required = false; } }
    function addRow(){ var table = document.getElementById("itemsTable").getElementsByTagName('tbody')[0]; var newRow = table.rows[0].cloneNode(true); var inputs = newRow.getElementsByTagName('input'); for(var i=0; i<inputs.length; i++) inputs[i].value = ''; newRow.getElementsByClassName('custom-unit')[0].classList.add('d-none'); table.appendChild(newRow); }
    function removeRow(btn){ var table = document.getElementById("itemsTable").getElementsByTagName('tbody')[0]; if(table.rows.length > 1) btn.closest('tr').remove(); }
    $('#indentForm').on('submit', function() { $('#submitBtn').prop('disabled', true).html('<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Saving...'); });
</script>
</body></html>
"""

HTML_EDIT = """
<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body class="bg-light">""" + HTML_NAV + """
<div class="container mt-5">
    <div class="card shadow mx-auto" style="max-width: 700px;">
        <div class="card-header d-flex justify-content-between"><h4>Edit Indent</h4> <span class="badge bg-light text-success">FY: {{ session.get('active_fy') }}</span></div>
        <div class="card-body">
            {% with messages = get_flashed_messages(with_categories=true) %}
              {% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }} shadow-sm">{{ message }}</div>{% endfor %}{% endif %}
            {% endwith %}
            <form method="POST" enctype="multipart/form-data">
                <div class="mb-3">
                    <label>Serial Number (Locked)</label>
                    <input type="text" value="{{ data.fy }}/{{ data.serial_no }}" class="form-control fw-bold" disabled>
                </div>
                
                <div class="row mb-3">
                    {% if session.get('permissions', {}).get('indent', {}).get('approve') or session['role'] == 'SuperAdmin' %}
                    <div class="col-md-6">
                        <div class="p-3 bg-warning bg-opacity-10 border border-warning rounded">
                            <label class="fw-bold">Approval Status</label>
                            {% if session['role'] == 'SuperAdmin' %}
                            <select name="approval_status" class="form-select" onchange="syncStatus()">
                                <option value="Pending" {% if data.approval_status == 'Pending' %}selected{% endif %}>Pending</option>
                                <option value="Approved" {% if data.approval_status == 'Approved' %}selected{% endif %}>Approved</option>
                                <option value="Hold" {% if data.approval_status == 'Hold' %}selected{% endif %}>Hold</option>
                                <option value="Rejected" {% if data.approval_status == 'Rejected' %}selected{% endif %}>Rejected</option>
                            </select>
                            {% else %}
                                {% if data.approval_status == 'Pending' %}
                                <select name="approval_status" class="form-select" onchange="syncStatus()">
                                    <option value="Pending" selected>Pending</option>
                                    <option value="Approved">Approved</option>
                                </select>
                                {% else %}
                                <input type="text" class="form-control fw-bold" value="{{ data.approval_status }}" disabled>
                                <input type="hidden" name="approval_status" value="{{ data.approval_status }}">
                                {% endif %}
                            {% endif %}
                        </div>
                    </div>
                    {% endif %}
                    
                    {% if session.get('permissions', {}).get('indent', {}).get('mark_received') or session['role'] == 'SuperAdmin' %}
                    <div class="col-md-6">
                        <div class="p-3 bg-info bg-opacity-10 border border-info rounded">
                            <label class="fw-bold">Received Status</label>
                            <select name="received_status" class="form-select" id="recStatus" onchange="toggleRecDate()">
                                <option value="Pending" {% if data.received_status == 'Pending' %}selected{% endif %}>Pending</option>
                                <option value="Received" {% if data.received_status == 'Received' %}selected{% endif %}>Received</option>
                                <option value="Rejected" {% if data.received_status == 'Rejected' %}selected{% endif %}>Rejected</option>
                            </select>
                            <input type="date" name="received_date" id="recDate" class="form-control mt-2 {% if data.received_status != 'Received' %}d-none{% endif %}" value="{{ data.received_date }}">
                        </div>
                    </div>
                    {% endif %}
                </div>
                
                <div class="row mb-3">
                    <div class="col-md-12 border p-3 bg-light rounded">
                        <label class="fw-bold text-success mb-2"><i class="bi bi-image"></i> Product Image (Max 200KB)</label>
                        {% if data.image_url and data.image_url != "" %}
                            <div class="mb-3 d-flex align-items-center bg-white p-2 rounded shadow-sm">
                                <button type="button" class="me-3 btn btn-sm btn-outline-info" onclick="viewImage('{{ data.image_url }}')">View Current Image</button>
                                <div class="form-check mb-0">
                                    <input class="form-check-input" type="checkbox" name="delete_image" value="1" id="delImg">
                                    <label class="form-check-label text-danger fw-bold" for="delImg">Delete Current Image</label>
                                </div>
                            </div>
                        {% endif %}
                        <input type="file" name="product_image" class="form-control" accept="image/*" onchange="validateFileSize(this)">
                        <small class="text-muted">Uploading a new file will replace the current image.</small>
                    </div>
                </div>

                <div class="row mb-3">
                    <div class="col-md-4"><label>Date</label><input type="date" name="indent_date" class="form-control" value="{{ data.indent_date }}" required></div>
                    <div class="col-md-4"><label>Department</label><input type="text" name="department" class="form-control" value="{{ data.department }}" required list="deptList"><datalist id="deptList">{% for r in departments %}<option value="{{ r }}">{% endfor %}</datalist></div>
                    <div class="col-md-4"><label>Indent Person Name</label><input type="text" name="indent_person" class="form-control" value="{{ data.indent_person }}" list="personList"><datalist id="personList">{% for p in persons %}<option value="{{ p }}">{% endfor %}</datalist></div>
                </div>
                <div class="mb-3"><label>Item</label><input type="text" name="item" class="form-control" value="{{ data.item }}" required></div>
                <div class="row mb-3">
                    <div class="col-md-6"><label>Reason</label><input type="text" name="reason" class="form-control" value="{{ data.reason }}"></div>
                    <div class="col-md-6"><label>Remarks</label><input type="text" name="remarks" class="form-control" value="{{ data.remarks }}"></div>
                </div>
                <div class="row mb-4">
                    <div class="col-md-4"><label>Quantity</label><input type="number" name="quantity" class="form-control" value="{{ data.quantity }}" required></div>
                    <div class="col-md-4"><label>Unit</label><select name="unit" class="form-select">{% for u in unit_list %}<option value="{{ u }}" {% if data.unit == u %}selected{% endif %}>{{ u }}</option>{% endfor %}</select></div>
                    <div class="col-md-4"><label>Assign To</label><select name="assigned_to" class="form-select">{% for user in users %}<option value="{{ user.name }}" {% if data.assigned_to == user.name %}selected{% endif %}>{{ user.name }}</option>{% endfor %}</select></div>
                </div>
                <button type="submit" class="btn btn-success w-100 py-2 fw-bold">Update Indent Details</button>
            </form>
        </div>
    </div>
</div>
<script>
    function toggleRecDate(){ var s = document.getElementById("recStatus").value; var d = document.getElementById("recDate"); if(s === "Received") d.classList.remove("d-none"); else d.classList.add("d-none"); }
    function syncStatus(){
        var appStatSel = document.querySelector('select[name="approval_status"]');
        if(!appStatSel) return;
        var appStat = appStatSel.value;
        var recStat = document.getElementById("recStatus");
        if(appStat === 'Rejected' && recStat){
            recStat.value = 'Rejected';
            toggleRecDate();
        }
    }
</script>
</body></html>
"""

HTML_REPORTS = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body>""" + HTML_NAV + """<div class="container mt-4"><h3 class="mb-4 no-print text-green">Indent Reports (FY {{ session.get('active_fy') }})</h3><div class="card shadow mb-4 no-print"><div class="card-body bg-light"><form method="POST" class="row g-3"><div class="col-md-2"><label>From</label><input type="date" name="start_date" class="form-control" value="{{ filters.start_date }}"></div><div class="col-md-2"><label>To</label><input type="date" name="end_date" class="form-control" value="{{ filters.end_date }}"></div><div class="col-md-2"><label>Department</label><input type="text" name="dept_filter" class="form-control" value="{{ filters.dept_filter }}"></div><div class="col-md-2"><label>Approval</label><select name="status" class="form-select"><option value="All">All</option><option value="Pending" {% if filters.status == 'Pending' %}selected{% endif %}>Pending</option><option value="Approved" {% if filters.status == 'Approved' %}selected{% endif %}>Approved</option><option value="Hold" {% if filters.status == 'Hold' %}selected{% endif %}>Hold</option><option value="Rejected" {% if filters.status == 'Rejected' %}selected{% endif %}>Rejected</option></select></div><div class="col-md-2"><label>Received</label><select name="received_status" class="form-select"><option value="All">All</option><option value="Received" {% if filters.received_status == 'Received' %}selected{% endif %}>Received</option><option value="Pending" {% if filters.received_status == 'Pending' %}selected{% endif %}>Pending Receipt</option><option value="Rejected" {% if filters.received_status == 'Rejected' %}selected{% endif %}>Rejected</option></select></div><div class="col-md-2"><label>Assigned To</label><select name="assigned_filter" class="form-select"><option value="All">All</option>{% for u in users %}<option value="{{ u.name }}" {% if filters.assigned_filter == u.name %}selected{% endif %}>{{ u.name }}</option>{% endfor %}</select></div><div class="col-md-2"><label>Sort By</label><select name="sort_by" class="form-select"><option value="Date" {% if filters.sort_by == 'Date' %}selected{% endif %}>Date</option><option value="Department" {% if filters.sort_by == 'Department' %}selected{% endif %}>Department</option><option value="Assigned" {% if filters.sort_by == 'Assigned' %}selected{% endif %}>Assigned Person</option></select></div><div class="col-md-10 text-end"><button type="submit" name="action" value="filter" class="btn btn-primary px-4">Filter</button><button type="submit" name="action" value="export" class="btn btn-success px-4">Export Excel</button></div></form></div></div><div class="d-none d-print-block"><h2>Report</h2><p>{{ current_time | date_fmt }}</p></div><div class="card shadow"><div class="card-header bg-white d-flex justify-content-between align-items-center no-print"><h5>Results ({{ indents|length }})</h5><button onclick="window.print()" class="btn btn-dark">Print</button></div><div class="card-body"><table class="table table-bordered table-striped table-sm"><thead class="table-dark"><tr><th>S.No (FY)</th><th>Date</th><th>Dept</th><th>Person</th><th>Item</th><th>Qty</th><th>Remarks</th><th>Assigned</th><th>Approved By</th><th>Status</th><th>Received</th><th class="no-print">Actions</th></tr></thead><tbody>{% for indent in indents %}<tr><td>{{ indent.fy }}/{{ indent.serial_no }}</td><td>{{ indent.indent_date | date_fmt }}</td><td>{{ indent.department }}</td><td>{{ indent.indent_person }}</td><td>{{ indent.item }}</td><td>{{ indent.quantity }} {{ indent.unit }}</td><td>{{ indent.remarks }}</td><td>{{ indent.assigned_to }}</td><td>{{ indent.approved_by_name if indent.approved_by_name else '' }}</td><td>{{ indent.approval_status }}</td><td>{% if indent.received_status == 'Received' %}Received ({{ indent.received_date | date_fmt }}){% else %}{{ indent.received_status }}{% endif %}</td><td class="no-print">{% if session.get('permissions', {}).get('indent', {}).get('edit') %}<a href="{{ url_for('edit_indent', i_id=indent.id) }}" class="btn btn-sm btn-outline-primary py-0">Edit</a>{% endif %}</td></tr>{% endfor %}</tbody></table></div></div></div></body></html>"""

# ==========================================
# PAYMENT TEMPLATES
# ==========================================
HTML_DASHBOARD_PAYMENT = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body>""" + HTML_NAV + """<div class="container-fluid mt-4 px-4"><div class="d-flex justify-content-between align-items-center mb-3 no-print"><h3 class="text-green fw-bold">Payment System <span class="badge bg-secondary ms-2" style="font-size: 0.5em; vertical-align: middle;">FY {{ session.get('active_fy') }}</span></h3>{% if session.get('permissions', {}).get('payment', {}).get('create') %}<a href="{{ url_for('create_payment') }}" class="btn btn-success">+ New Payment Entry</a>{% endif %}</div><div class="card shadow"><div class="card-body"><table class="table table-hover table-bordered align-middle table-sm"><thead class="table-light"><tr><th>SR.NO</th><th>TYPE</th><th>PARTY NAME</th><th>DETAILS</th><th>AMOUNT</th><th>APPROVED BY</th><th>STATUS</th><th class="no-print">ACTIONS</th></tr></thead><tbody>{% for p in payments %}<tr><td class="fw-bold">{{ p.fy }}/{{ p.serial_no }}</td><td>{% if p.type == 'Advance' %}<span class="badge bg-info text-dark">Advance/PO</span>{% else %}<span class="badge bg-secondary">Bill</span>{% endif %}</td><td>{{ p.party_name }}</td><td>{% if p.type == 'Advance' %}Qt: {{ p.quotation_no }} | {{ p.item_detail }}<br><span class="text-muted small">Delivery: {{ p.delivery_time }}</span>{% else %}Bill: {{ p.bill_number }}<br><span class="text-muted small">Due: {{ p.due_date | date_fmt }}</span>{% endif %}</td><td class="fw-bold text-end">{{ p.amount }}</td><td>{{ p.approved_by }}</td><td>{% if p.status == 'Done' %}<span class="badge bg-success">Done</span>{% else %}<span class="badge bg-danger">Pending</span>{% endif %}</td><td class="no-print">{% if session.get('permissions', {}).get('payment', {}).get('edit') %}<a href="{{ url_for('edit_payment', p_id=p.id) }}" class="btn btn-sm btn-outline-primary"><i class="bi bi-pencil-square"></i></a>{% endif %}{% if session.get('permissions', {}).get('payment', {}).get('delete') %}<a href="{{ url_for('delete_payment', p_id=p.id) }}" class="btn btn-sm btn-outline-danger" onclick="return confirm('Are you sure?')"><i class="bi bi-trash"></i></a>{% endif %}</td></tr>{% else %}<tr><td colspan="8" class="text-center">No records for FY {{ session.get('active_fy') }}.</td></tr>{% endfor %}</tbody></table></div></div></div></body></html>"""

HTML_CREATE_PAYMENT = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body class="bg-light">""" + HTML_NAV + """<div class="container mt-5"><div class="card shadow mx-auto" style="max-width: 800px;"><div class="card-header d-flex justify-content-between"><h4>New Payment / Order</h4> <span class="badge bg-light text-success">FY: {{ session.get('active_fy') }}</span></div><div class="card-body"><ul class="nav nav-tabs mb-4" id="paymentTabs"><li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#regularBill" onclick="setMode('Bill')">Regular Bill Entry</a></li><li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#advanceOrder" onclick="setMode('Advance')">Advance / PO Entry</a></li></ul>{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }} shadow-sm">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}<form method="POST"><input type="hidden" name="entry_type" id="entryType" value="Bill"><div class="tab-content"><div class="tab-pane fade show active" id="regularBill"><div class="mb-3"><label class="fw-bold">Party Name</label><input type="text" name="party_name" class="form-control" placeholder="Vendor Name"></div><div class="row mb-3"><div class="col-md-6"><label class="fw-bold">Bill Number</label><input type="text" name="bill_number" class="form-control"></div><div class="col-md-6"><label class="fw-bold">Bill Date</label><input type="date" name="bill_date" class="form-control" value="{{ today }}"></div></div><div class="row mb-3"><div class="col-md-6"><label class="fw-bold">Amount</label><input type="number" step="0.01" name="amount" class="form-control"></div><div class="col-md-6"><label class="fw-bold">Due Date</label><input type="date" name="due_date" class="form-control"></div></div></div><div class="tab-pane fade" id="advanceOrder"><div class="row mb-3"><div class="col-md-6"><label class="fw-bold">Party Name</label><input type="text" name="adv_party_name" class="form-control"></div><div class="col-md-6"><label class="fw-bold">Quotation No.</label><input type="text" name="quotation_no" class="form-control"></div></div><div class="p-3 bg-light border rounded mb-3"><h6 class="text-primary">Product Details</h6><div class="mb-2"><label>Product Name / Detail</label><input type="text" name="item_detail" class="form-control"></div><div class="row"><div class="col-md-3"><label>Qty</label><input type="number" step="0.01" name="qty" id="qty" class="form-control" oninput="calcTotal()"></div><div class="col-md-3"><label>Price</label><input type="number" step="0.01" name="price" id="price" class="form-control" oninput="calcTotal()"></div><div class="col-md-3"><label>Tax</label><input type="number" step="0.01" name="tax" id="tax" class="form-control" oninput="calcTotal()" value="0"></div><div class="col-md-3"><label>Freight</label><input type="number" step="0.01" name="freight" id="freight" class="form-control" oninput="calcTotal()" value="0"></div></div><div class="mt-2 text-end"><h5>Total: <span id="totalDisplay">0.00</span></h5><input type="hidden" name="adv_amount" id="advAmount"></div></div><div class="row mb-3"><div class="col-md-6"><label class="fw-bold">Payment Type</label><select name="payment_type" class="form-select" onchange="toggleBank(this)"><option value="Credit">Credit</option><option value="Advance">Advance</option></select></div><div class="col-md-6"><label class="fw-bold">Delivery Time</label><input type="text" name="delivery_time" class="form-control" placeholder="e.g. 7 Days"></div></div><div id="bankDetails" class="d-none p-3 border border-warning rounded bg-warning bg-opacity-10 mb-3"><h6>Bank Details (Required for Advance)</h6><div class="row"><div class="col-md-3"><label class="small fw-bold">Bank Name</label><input type="text" name="bank_name" class="form-control" placeholder="Bank Name"></div><div class="col-md-3"><label class="small fw-bold">Branch Name</label><input type="text" name="branch_name" class="form-control" placeholder="Branch"></div><div class="col-md-3"><label class="small fw-bold">Account No</label><input type="text" name="account_no" class="form-control" placeholder="Account No"></div><div class="col-md-3"><label class="small fw-bold">IFSC Code</label><input type="text" name="ifsc" class="form-control" placeholder="IFSC Code"></div></div></div></div></div><div class="mb-3 mt-3"><label class="fw-bold">Approved By</label><input type="text" name="approved_by" class="form-control" required placeholder="Enter Name"></div><button type="submit" class="btn btn-success w-100">Save Entry</button><a href="{{ url_for('payment_dashboard') }}" class="btn btn-secondary w-100 mt-2">Cancel</a></form></div></div></div><script>function setMode(mode){document.getElementById('entryType').value=mode;}function toggleBank(select){var bankDiv=document.getElementById('bankDetails');if(select.value==='Advance')bankDiv.classList.remove('d-none');else bankDiv.classList.add('d-none');}function calcTotal(){var qty=parseFloat(document.getElementById('qty').value)||0;var price=parseFloat(document.getElementById('price').value)||0;var tax=parseFloat(document.getElementById('tax').value)||0;var freight=parseFloat(document.getElementById('freight').value)||0;var total=(qty*price)+tax+freight;document.getElementById('totalDisplay').innerText=total.toFixed(2);document.getElementById('advAmount').value=total.toFixed(2);}</script></body></html>"""
HTML_EDIT_PAYMENT = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body class="bg-light">""" + HTML_NAV + """<div class="container mt-5"><div class="card shadow mx-auto" style="max-width: 700px;"><div class="card-header d-flex justify-content-between"><h4>Edit Payment</h4> <span class="badge bg-light text-success">FY: {{ session.get('active_fy') }}</span></div><div class="card-body">{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }} shadow-sm">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}<form method="POST"><div class="mb-3"><label>Serial Number (FY)</label><input type="text" value="{{ data.fy }}/{{ data.serial_no }}" class="form-control fw-bold" disabled></div>{% if data.type == 'Advance' %}<div class="alert alert-info">Advance Order Entry</div><div class="row mb-2"><div class="col-md-6"><label>Party Name</label><input type="text" name="party_name" class="form-control" value="{{ data.party_name }}"></div><div class="col-md-6"><label>Quotation No</label><input type="text" name="quotation_no" class="form-control" value="{{ data.quotation_no }}"></div></div><div class="row mb-2"><div class="col-md-6"><label>Item</label><input type="text" name="item_detail" class="form-control" value="{{ data.item_detail }}"></div><div class="col-md-6"><label>Amount</label><input type="number" step="0.01" name="amount" class="form-control" value="{{ data.amount }}"></div></div><div class="row mb-2"><div class="col-md-4"><label>Qty</label><input type="text" name="qty" class="form-control" value="{{ data.qty }}"></div><div class="col-md-4"><label>Price</label><input type="text" name="price" class="form-control" value="{{ data.price }}"></div><div class="col-md-4"><label>Tax</label><input type="text" name="tax" class="form-control" value="{{ data.tax }}"></div></div><div class="row mb-2"><div class="col-md-6"><label>Payment Type</label><input type="text" name="payment_type" class="form-control" value="{{ data.payment_type }}"></div><div class="col-md-6"><label>Delivery Time</label><input type="text" name="delivery_time" class="form-control" value="{{ data.delivery_time }}"></div></div><div class="mb-2"><label>Bank Details</label><input type="text" name="bank_details" class="form-control" value="{{ data.bank_details }}"></div>{% else %}<div class="alert alert-secondary">Regular Bill Entry</div><div class="row mb-3"><div class="col-md-12 mb-2"><label class="fw-bold">Party Name</label><input type="text" name="party_name" class="form-control" value="{{ data.party_name }}" required></div><div class="col-md-6 mb-2"><label class="fw-bold">Bill Number</label><input type="text" name="bill_number" class="form-control" value="{{ data.bill_number }}" required></div><div class="col-md-6 mb-2"><label class="fw-bold">Bill Date</label><input type="date" name="bill_date" class="form-control" value="{{ data.bill_date }}" required></div><div class="col-md-6"><label class="fw-bold">Amount</label><input type="number" step="0.01" name="amount" class="form-control" value="{{ data.amount }}" required></div><div class="col-md-6"><label class="fw-bold">Due Date</label><input type="date" name="due_date" class="form-control" value="{{ data.due_date }}" required></div></div>{% endif %}<div class="col-md-12 mt-2"><label class="fw-bold">Approved By (Manual)</label><input type="text" name="approved_by" class="form-control" value="{{ data.approved_by }}" required></div><div class="mb-3 mt-3 p-3 border rounded border-warning bg-warning bg-opacity-10"><h5 class="text-dark">Status & Payment</h5><div class="mb-3"><label class="fw-bold">Status</label><select name="status" class="form-select" id="statusSelect" onchange="toggleDetails()"><option value="Pending" {% if data.status == 'Pending' %}selected{% endif %}>Pending</option><option value="Done" {% if data.status == 'Done' %}selected{% endif %}>Done (Paid)</option></select></div><div id="paymentDetails" class="{% if data.status != 'Done' %}d-none{% endif %}"><div class="row"><div class="col-md-6 mb-2"><label class="fw-bold">Payment Date</label><input type="date" name="payment_date" class="form-control" value="{{ data.payment_date }}"></div><div class="col-md-6 mb-2"><label class="fw-bold">Mode</label><select name="payment_mode" class="form-select"><option value="" selected disabled>Select</option><option value="NEFT" {% if data.payment_mode == 'NEFT' %}selected{% endif %}>NEFT</option><option value="RTGS" {% if data.payment_mode == 'RTGS' %}selected{% endif %}>RTGS</option><option value="UPI" {% if data.payment_mode == 'UPI' %}selected{% endif %}>UPI</option><option value="CHEQUE" {% if data.payment_mode == 'CHEQUE' %}selected{% endif %}>CHEQUE</option><option value="CASH" {% if data.payment_mode == 'CASH' %}selected{% endif %}>CASH</option></select></div><div class="col-md-12"><label class="fw-bold">Ref No.</label><input type="text" name="transaction_ref" class="form-control" value="{{ data.transaction_ref }}"></div></div></div></div><button type="submit" class="btn btn-success w-100">Update</button><a href="{{ url_for('payment_dashboard') }}" class="btn btn-secondary w-100 mt-2">Cancel</a></form></div></div></div><script>function toggleDetails(){var status=document.getElementById("statusSelect").value;var detailsDiv=document.getElementById("paymentDetails");if(status==="Done")detailsDiv.classList.remove("d-none");else detailsDiv.classList.add("d-none");}</script></body></html>"""
HTML_REPORTS_PAYMENT = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body>""" + HTML_NAV + """<div class="container mt-4"><h3 class="mb-4 no-print text-green">Payment Reports (FY {{ session.get('active_fy') }})</h3><div class="card shadow mb-4 no-print"><div class="card-body bg-light"><form method="POST" class="row g-3"><div class="col-md-2"><label>From</label><input type="date" name="start_date" class="form-control" value="{{ filters.start_date }}"></div><div class="col-md-2"><label>To</label><input type="date" name="end_date" class="form-control" value="{{ filters.end_date }}"></div><div class="col-md-2"><label>Party Name</label><input type="text" name="party_filter" class="form-control" value="{{ filters.party_filter }}"></div><div class="col-md-2"><label>Status</label><select name="status" class="form-select"><option value="All">All</option><option value="Pending" {% if filters.status == 'Pending' %}selected{% endif %}>Pending</option><option value="Done" {% if filters.status == 'Done' %}selected{% endif %}>Done</option></select></div><div class="col-md-2 d-flex align-items-end gap-2"><button type="submit" name="action" value="filter" class="btn btn-primary w-50">Filter</button><button type="submit" name="action" value="export" class="btn btn-success w-50">Excel</button></div></form></div></div><div class="d-none d-print-block"><h2>Payment Report</h2><p>{{ current_time | date_fmt }}</p></div><div class="card shadow"><div class="card-header bg-white d-flex justify-content-between align-items-center no-print"><h5>Results ({{ payments|length }})</h5><button onclick="window.print()" class="btn btn-dark">Print</button></div><div class="card-body"><table class="table table-bordered table-striped table-sm align-middle"><thead class="table-dark"><tr><th>SR</th><th>TYPE</th><th>PARTY</th><th>REF/BILL</th><th>ITEM/DETAILS</th><th>AMOUNT</th><th>STATUS</th></tr></thead><tbody>{% for p in payments %}<tr><td>{{ p.fy }}/{{ p.serial_no }}</td><td>{{ p.type }}</td><td>{{ p.party_name }}</td><td>{% if p.type == 'Advance' %}Qt: {{ p.quotation_no }}{% else %}Bill: {{ p.bill_number }}{% endif %}</td><td>{% if p.type == 'Advance' %}{{ p.item_detail }} (Qty: {{ p.qty }}){% else %}Bill Date: {{ p.bill_date | date_fmt }}{% endif %}</td><td>{{ p.amount }}</td><td>{{ p.status }}</td></tr>{% endfor %}</tbody></table></div></div></div></body></html>"""

# ==========================================
# GATEPASS TEMPLATES
# ==========================================
HTML_DASHBOARD_GATEPASS = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body>""" + HTML_NAV + """<div class="container-fluid mt-4 px-4"><div class="d-flex justify-content-between align-items-center mb-3 no-print"><h3 class="text-green fw-bold">Gate Pass System <span class="badge bg-secondary ms-2" style="font-size: 0.5em; vertical-align: middle;">FY {{ session.get('active_fy') }}</span></h3><div class="d-flex gap-2 align-items-center"><form method="GET" class="d-flex"><div class="input-group" style="width: 250px;"><input type="text" name="search" class="form-control form-control-sm" placeholder="Search Company, Product, Person..." value="{{ request.args.get('search', '') }}"><button class="btn btn-primary btn-sm" type="submit"><i class="bi bi-search"></i></button>{% if request.args.get('search') %}<a href="{{ url_for('gatepass_dashboard') }}" class="btn btn-outline-secondary btn-sm">Reset</a>{% endif %}</div></form>{% if session.get('permissions', {}).get('gatepass', {}).get('create') %}<a href="{{ url_for('create_gatepass') }}" class="btn btn-success">+ New Gate Pass</a>{% endif %}</div></div>{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category if category != 'message' else 'info' }} shadow-sm">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}<div class="card shadow"><div class="card-body p-0"><div class="table-responsive"><table class="table table-hover table-bordered align-middle table-sm mb-0"><thead class="table-light"><tr><th>SR.NO</th><th>TYPE</th><th>OUT DATE</th><th>COMPANY</th><th>PRODUCT / QTY</th><th>CARRIER (BY HAND)</th><th>REASON / REMARK</th><th>STATUS / CLEARED</th><th class="no-print">ACTIONS</th></tr></thead><tbody>{% for gp in gatepasses %}<tr class="{% if gp.status == 'Cleared' %}status-cleared{% endif %}"><td class="fw-bold">{{ gp.fy }}/{{ gp.serial_no }}</td><td>{% if gp.type == 'RGP' %}<span class="badge bg-warning text-dark">RGP</span>{% else %}<span class="badge bg-secondary">NRGP</span>{% endif %}</td><td>{{ gp.out_date | date_fmt }}</td><td class="fw-bold">{{ gp.company_name }}</td><td><span class="text-green fw-bold">{{ gp.product }}</span><br><small class="text-muted">Qty: {{ gp.qty }}</small></td><td>{{ gp.by_hand_person }}<br><small class="text-muted fst-italic">{{ gp.purpose }}</small></td><td><small class="d-block text-muted"><strong>Rsn:</strong> {{ gp.reason }}</small><small class="d-block text-muted"><strong>Rmk:</strong> {{ gp.remark }}</small></td><td>{% if gp.status == 'Cleared' %}<span class="badge bg-success">Cleared</span><div class="small-meta">On: {{ gp.clear_date | date_fmt }}</div><div class="small-meta">By: {{ gp.clear_by }}</div>{% else %}<span class="badge bg-danger">Pending</span>{% endif %}</td><td class="no-print">{% if session.get('permissions', {}).get('gatepass', {}).get('edit') %}<a href="{{ url_for('edit_gatepass', gp_id=gp.id) }}" class="btn btn-sm btn-outline-primary py-0"><i class="bi bi-pencil-square"></i></a>{% endif %}{% if session.get('permissions', {}).get('gatepass', {}).get('delete') %}<a href="{{ url_for('delete_gatepass', gp_id=gp.id) }}" class="btn btn-sm btn-outline-danger py-0" onclick="return confirm('Delete Gate Pass?')"><i class="bi bi-trash"></i></a>{% endif %}</td></tr>{% else %}<tr><td colspan="9" class="text-center py-4">No Gate Pass records for FY {{ session.get('active_fy') }}.</td></tr>{% endfor %}</tbody></table></div></div></div></div></body></html>"""
HTML_CREATE_GATEPASS = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body class="bg-light">""" + HTML_NAV + """<div class="container mt-5"><div class="card shadow mx-auto" style="max-width: 800px;"><div class="card-header d-flex justify-content-between"><h4>New Gate Pass Entry</h4> <span class="badge bg-light text-success">FY: {{ session.get('active_fy') }}</span></div><div class="card-body">{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }} shadow-sm">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}<form method="POST"><div class="row mb-3"><div class="col-md-4"><label class="fw-bold">Pass Type</label><select name="gp_type" class="form-select" required><option value="RGP">RGP (Returnable)</option><option value="NRGP">NRGP (Non-Returnable)</option></select></div><div class="col-md-4"><label class="fw-bold">Out Date (Pending From)</label><input type="date" name="out_date" class="form-control" value="{{ today }}" required></div><div class="col-md-4"><label class="fw-bold">Company Name</label><select name="company_select" class="form-select" onchange="checkCompany(this)" required><option value="" disabled selected>Select Company</option>{% for c in companies %}<option value="{{ c }}">{{ c }}</option>{% endfor %}<option value="Other">Other (Add New)</option></select><input type="text" name="custom_company" class="form-control mt-2 d-none" placeholder="Enter New Company" id="customCompanyInput"></div></div><div class="row mb-3"><div class="col-md-5"><label class="fw-bold">Product / Item</label><input type="text" name="product" class="form-control" required></div><div class="col-md-3"><label class="fw-bold">Quantity</label><input type="text" name="qty" class="form-control" required></div><div class="col-md-4"><label class="fw-bold">By Hand Person Name</label><select name="by_hand_person" class="form-select" required><option value="" disabled selected>Select Person</option>{% for u in users %}<option value="{{ u.name }}">{{ u.name }}</option>{% endfor %}</select></div></div><div class="row mb-3"><div class="col-md-4"><label class="fw-bold">Purpose</label><input type="text" name="purpose" class="form-control"></div><div class="col-md-4"><label class="fw-bold">Reason</label><input type="text" name="reason" class="form-control"></div><div class="col-md-4"><label class="fw-bold">Remark</label><input type="text" name="remark" class="form-control"></div></div><button type="submit" class="btn btn-success w-100">Create Gate Pass</button><a href="{{ url_for('gatepass_dashboard') }}" class="btn btn-secondary w-100 mt-2">Cancel</a></form></div></div></div><script>function checkCompany(selectObj) { var customInput = document.getElementById('customCompanyInput'); if(selectObj.value === 'Other') { customInput.classList.remove('d-none'); customInput.required = true; customInput.focus(); } else { customInput.classList.add('d-none'); customInput.required = false; } }</script></body></html>"""
HTML_EDIT_GATEPASS = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body class="bg-light">""" + HTML_NAV + """<div class="container mt-5"><div class="card shadow mx-auto" style="max-width: 800px;"><div class="card-header d-flex justify-content-between"><h4>Edit Gate Pass</h4> <span class="badge bg-light text-success">FY: {{ session.get('active_fy') }}</span></div><div class="card-body">{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }} shadow-sm">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}<form method="POST"><div class="mb-3"><label>Serial Number (FY)</label><input type="text" value="{{ data.fy }}/{{ data.serial_no }}" class="form-control fw-bold" disabled></div><div class="row mb-3 p-3 bg-warning bg-opacity-10 border border-warning rounded"><div class="col-md-4"><label class="fw-bold">Clearance Status</label><select name="status" id="gpStatus" class="form-select" onchange="toggleClear()"><option value="Pending" {% if data.status == 'Pending' %}selected{% endif %}>Pending</option><option value="Cleared" {% if data.status == 'Cleared' %}selected{% endif %}>Cleared</option></select></div><div class="col-md-4"><label class="fw-bold">Clear Date</label><input type="date" name="clear_date" id="clearDate" class="form-control {% if data.status != 'Cleared' %}d-none{% endif %}" value="{{ data.clear_date }}"></div><div class="col-md-4"><label class="fw-bold">Cleared By / Purpose</label><input type="text" name="clear_by" id="clearBy" class="form-control {% if data.status != 'Cleared' %}d-none{% endif %}" value="{{ data.clear_by }}" placeholder="Person who received it back"></div></div><div class="row mb-3"><div class="col-md-4"><label class="fw-bold">Pass Type</label><select name="gp_type" class="form-select" required><option value="RGP" {% if data.type == 'RGP' %}selected{% endif %}>RGP</option><option value="NRGP" {% if data.type == 'NRGP' %}selected{% endif %}>NRGP</option></select></div><div class="col-md-4"><label class="fw-bold">Out Date</label><input type="date" name="out_date" class="form-control" value="{{ data.out_date }}" required></div><div class="col-md-4"><label class="fw-bold">Company Name</label><input type="text" name="company_name" class="form-control" value="{{ data.company_name }}" required list="companyList"><datalist id="companyList">{% for c in companies %}<option value="{{ c }}">{% endfor %}</datalist></div></div><div class="row mb-3"><div class="col-md-5"><label class="fw-bold">Product / Item</label><input type="text" name="product" class="form-control" value="{{ data.product }}" required></div><div class="col-md-3"><label class="fw-bold">Quantity</label><input type="text" name="qty" class="form-control" value="{{ data.qty }}" required></div><div class="col-md-4"><label class="fw-bold">By Hand Person Name</label><select name="by_hand_person" class="form-select" required>{% for u in users %}<option value="{{ u.name }}" {% if data.by_hand_person == u.name %}selected{% endif %}>{{ u.name }}</option>{% endfor %}</select></div></div><div class="row mb-3"><div class="col-md-4"><label class="fw-bold">Purpose</label><input type="text" name="purpose" class="form-control" value="{{ data.purpose }}"></div><div class="col-md-4"><label class="fw-bold">Reason</label><input type="text" name="reason" class="form-control" value="{{ data.reason }}"></div><div class="col-md-4"><label class="fw-bold">Remark</label><input type="text" name="remark" class="form-control" value="{{ data.remark }}"></div></div><button type="submit" class="btn btn-success w-100">Update Gate Pass</button><a href="{{ url_for('gatepass_dashboard') }}" class="btn btn-secondary w-100 mt-2">Cancel</a></form></div></div></div><script>function toggleClear(){var s=document.getElementById("gpStatus").value;var d=document.getElementById("clearDate");var b=document.getElementById("clearBy");if(s==="Cleared"){d.classList.remove("d-none");b.classList.remove("d-none");if(!d.value){var today = new Date(); d.value = today.toISOString().split('T')[0];}}else{d.classList.add("d-none");b.classList.add("d-none");}}</script></body></html>"""
HTML_REPORTS_GATEPASS = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body>""" + HTML_NAV + """<div class="container mt-4"><h3 class="mb-4 no-print text-green">Gate Pass Reports (FY {{ session.get('active_fy') }})</h3><div class="card shadow mb-4 no-print"><div class="card-body bg-light"><form method="POST" class="row g-3"><div class="col-md-2"><label>From</label><input type="date" name="start_date" class="form-control" value="{{ filters.start_date }}"></div><div class="col-md-2"><label>To</label><input type="date" name="end_date" class="form-control" value="{{ filters.end_date }}"></div><div class="col-md-2"><label>Type</label><select name="gp_type" class="form-select"><option value="All">All</option><option value="RGP" {% if filters.gp_type == 'RGP' %}selected{% endif %}>RGP</option><option value="NRGP" {% if filters.gp_type == 'NRGP' %}selected{% endif %}>NRGP</option></select></div><div class="col-md-2"><label>Status</label><select name="status" class="form-select"><option value="All">All</option><option value="Pending" {% if filters.status == 'Pending' %}selected{% endif %}>Pending</option><option value="Cleared" {% if filters.status == 'Cleared' %}selected{% endif %}>Cleared</option></select></div><div class="col-md-2"><label>Company / Person</label><input type="text" name="search_filter" class="form-control" placeholder="Search..." value="{{ filters.search_filter }}"></div><div class="col-md-2 d-flex align-items-end gap-2"><button type="submit" name="action" value="filter" class="btn btn-primary w-50">Filter</button><button type="submit" name="action" value="export" class="btn btn-success w-50">Excel</button></div></form></div></div><div class="d-none d-print-block"><h2>Gate Pass Report</h2><p>{{ current_time | date_fmt }}</p></div><div class="card shadow"><div class="card-header bg-white d-flex justify-content-between align-items-center no-print"><h5>Results ({{ gatepasses|length }})</h5><button onclick="window.print()" class="btn btn-dark">Print</button></div><div class="card-body"><table class="table table-bordered table-striped table-sm align-middle"><thead class="table-dark"><tr><th>SR</th><th>TYPE</th><th>OUT DATE</th><th>COMPANY</th><th>PRODUCT / QTY</th><th>CARRIER / PURPOSE</th><th>STATUS</th><th>CLEAR DATE / BY</th></tr></thead><tbody>{% for gp in gatepasses %}<tr><td>{{ gp.fy }}/{{ gp.serial_no }}</td><td>{{ gp.type }}</td><td>{{ gp.out_date | date_fmt }}</td><td>{{ gp.company_name }}</td><td>{{ gp.product }} (Qty: {{ gp.qty }})</td><td>{{ gp.by_hand_person }}<br><small>{{ gp.purpose }}</small></td><td>{{ gp.status }}</td><td>{% if gp.status == 'Cleared' %}{{ gp.clear_date | date_fmt }} ({{ gp.clear_by }}){% else %}-{% endif %}</td></tr>{% endfor %}</tbody></table></div></div></div></body></html>"""

# ==========================================
# SETTINGS TEMPLATES
# ==========================================
HTML_SETTINGS = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body>""" + HTML_NAV + """<div class="container mt-4"><h2 class="mb-4 text-green">Admin Settings</h2><ul class="nav nav-tabs" id="myTab" role="tablist"><li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#fys">Financial Years</button></li><li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#units">Manage Units</button></li><li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#companies">Manage Companies</button></li><li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#users">Manage Users</button></li><li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#logs">Login Logs</button></li><li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#maintenance">Maintenance <i class="bi bi-tools text-danger"></i></button></li></ul><div class="tab-content pt-4">
<div class="tab-pane fade show active" id="fys"><div class="row"><div class="col-md-5"><div class="card shadow"><div class="card-header bg-warning text-dark fw-bold">Create New Financial Year</div><div class="card-body"><form method="POST" action="{{ url_for('add_fy') }}"><div class="mb-2"><label>Format: YYYY-YY (e.g. 2026-27)</label><input type="text" name="fy_name" class="form-control" placeholder="2026-27" required></div><button class="btn btn-warning w-100" type="submit">Create FY</button></form></div></div></div><div class="col-md-7"><div class="card shadow"><div class="card-header">Available Financial Years</div><div class="card-body"><table class="table table-sm"><thead><tr><th>FY Name</th><th>Action</th></tr></thead><tbody>{% for fy in available_fys %}<tr><td class="fw-bold">{{ fy }}</td><td><a href="{{ url_for('delete_fy', fy_name=fy) }}" class="btn btn-sm btn-outline-danger" onclick="return confirm('Caution: Deleting this removes it from the dropdown. Records will not be deleted, but they may become inaccessible from the UI. Continue?')">Delete</a></td></tr>{% endfor %}</tbody></table></div></div></div></div></div>
<div class="tab-pane fade" id="units"><div class="row"><div class="col-md-5"><div class="card shadow"><div class="card-header bg-secondary text-white">Add New Unit</div><div class="card-body"><form method="POST" action="{{ url_for('add_unit') }}"><div class="input-group"><input type="text" name="unit_name" class="form-control" placeholder="e.g. PACKET" required><button class="btn btn-success" type="submit">Add</button></div></form></div></div></div><div class="col-md-7"><div class="card shadow"><div class="card-header">Existing Units</div><div class="card-body"><table class="table table-sm"><thead><tr><th>Unit Name</th><th>Action</th></tr></thead><tbody>{% for u in units %}<tr><td>{{ u.name }}</td><td><a href="{{ url_for('delete_unit', uid=u.id) }}" class="btn btn-sm btn-outline-danger">Delete</a></td></tr>{% endfor %}</tbody></table></div></div></div></div></div>
<div class="tab-pane fade" id="companies"><div class="row"><div class="col-md-5"><div class="card shadow"><div class="card-header bg-primary text-white">Add New Company</div><div class="card-body"><form method="POST" action="{{ url_for('add_company') }}"><div class="input-group"><input type="text" name="company_name" class="form-control" placeholder="e.g. ABC CORP" required><button class="btn btn-success" type="submit">Add</button></div></form></div></div></div><div class="col-md-7"><div class="card shadow"><div class="card-header">Existing Companies</div><div class="card-body"><table class="table table-sm"><thead><tr><th>Company Name</th><th>Action</th></tr></thead><tbody>{% for c in companies %}<tr><td>{{ c.name }}</td><td><a href="{{ url_for('delete_company', cid=c.id) }}" class="btn btn-sm btn-outline-danger">Delete</a></td></tr>{% endfor %}</tbody></table></div></div></div></div></div>
<div class="tab-pane fade" id="users"><div class="d-flex justify-content-end mb-2"><a href="{{ url_for('edit_user', uid='new') }}" class="btn btn-success">+ Create User</a></div><div class="card shadow"><div class="card-body"><table class="table"><thead class="table-dark"><tr><th>Name</th><th>Username</th><th>Role</th><th>Password</th><th>Actions</th></tr></thead><tbody>{% for user in users %}<tr><td>{{ user.name }}</td><td>{{ user.username }}</td><td>{{ user.role }}</td><td class="font-monospace">{% if session['role'] == 'SuperAdmin' %}<span class="text-danger">{{ user.password }}</span>{% else %}******{% endif %}</td><td><a href="{{ url_for('edit_user', uid=user.id) }}" class="btn btn-sm btn-primary">Edit</a>{% if session['role'] == 'SuperAdmin' %}<a href="{{ url_for('delete_user', uid=user.id) }}" class="btn btn-sm btn-danger" onclick="return confirm('Delete?')">Delete</a>{% elif session['role'] == 'Admin' and user.role not in ['Admin', 'SuperAdmin'] %}<a href="{{ url_for('delete_user', uid=user.id) }}" class="btn btn-sm btn-danger" onclick="return confirm('Delete?')">Delete</a>{% endif %}</td></tr>{% endfor %}</tbody></table></div></div></div><div class="tab-pane fade" id="logs"><div class="card shadow"><div class="card-header bg-info text-white">Recent Logins (Last 50)</div><div class="card-body">{% if session['role'] == 'SuperAdmin' %}<table class="table table-striped table-sm"><thead><tr><th>Time</th><th>Name</th><th>Username</th><th>Role</th></tr></thead><tbody>{% for log in logs %}<tr><td>{{ log.timestamp | datetime_fmt }}</td><td>{{ log.name }}</td><td>{{ log.username }}</td><td>{{ log.role }}</td></tr>{% else %}<tr><td colspan="4" class="text-center">No logs found</td></tr>{% endfor %}</tbody></table>{% else %}<div class="alert alert-warning text-center">Only SuperAdmin can view logs.</div>{% endif %}</div></div></div>
<div class="tab-pane fade" id="maintenance">
    <div class="card shadow border-danger mb-4">
        <div class="card-header bg-danger text-white fw-bold"><i class="bi bi-exclamation-triangle-fill me-2"></i>Database Maintenance & Serial Fix</div>
        <div class="card-body">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}{% for category, message in messages %}{% if category in ['success', 'warning', 'danger'] %}
                    <div class="alert alert-{{ category }} shadow-sm">{{ message }}</div>
                {% endif %}{% endfor %}{% endif %}
            {% endwith %}
            <div class="alert alert-warning"><strong>Notice:</strong> Use this tool if you have duplicate serial numbers or if numbers are out of order. This will <strong>sort all entries chronologically by Date</strong> and permanently re-assign serial numbers sequentially starting from 1.</div>
            <form method="POST" action="{{ url_for('fix_serials') }}" onsubmit="return confirm('Are you sure you want to completely re-sequence the serial numbers for this Financial Year? This action cannot be undone.');">
                <div class="row align-items-end">
                    <div class="col-md-4"><label class="fw-bold">Select Collection</label><select name="collection_name" class="form-select" required><option value="indents">Indents System</option><option value="payments">Payments System</option><option value="gatepasses">Gate Pass System</option></select></div>
                    <div class="col-md-4"><label class="fw-bold">Financial Year</label><select name="fy_name" class="form-select" required>{% for fy in available_fys %}<option value="{{ fy }}">{{ fy }}</option>{% endfor %}</select></div>
                    <div class="col-md-4"><button type="submit" class="btn btn-danger w-100 fw-bold"><i class="bi bi-tools"></i> Re-Sort & Fix Serials by Date</button></div>
                </div>
            </form>
        </div>
    </div>
    
    <hr class="my-4">
    <h5 class="text-danger fw-bold"><i class="bi bi-cloud-arrow-down-fill"></i> Database Backup & Restore (SuperAdmin Only)</h5>
    <div class="row mt-3">
        <div class="col-md-6">
            <div class="p-3 border rounded bg-light h-100">
                <h6 class="text-primary"><i class="bi bi-download"></i> Download Full Backup</h6>
                <p class="small text-muted">Export a complete JSON backup of all system data. Keep this file safe before making major changes or moving servers.</p>
                <a href="{{ url_for('backup_database') }}" class="btn btn-outline-primary w-100 mt-2">Download Database.json</a>
            </div>
        </div>
        <div class="col-md-6">
            <div class="p-3 border rounded border-danger bg-danger bg-opacity-10 h-100">
                <h6 class="text-danger"><i class="bi bi-upload"></i> Restore from Backup</h6>
                <p class="small text-muted">Upload a previous JSON backup to restore data. <strong>Warning:</strong> This will overwrite existing records with the exact same ID.</p>
                <form method="POST" action="{{ url_for('restore_database') }}" enctype="multipart/form-data" onsubmit="return confirm('Are you absolutely sure? This will forcefully write backup data into the live database.');">
                    <div class="input-group mt-2">
                        <input type="file" name="backup_file" class="form-control form-control-sm" accept=".json" required>
                        <button class="btn btn-danger btn-sm px-3" type="submit">Restore Data</button>
                    </div>
                </form>
            </div>
        </div>
    </div>
</div>
</div></div><script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script></body></html>"""

HTML_EDIT_USER = """<!DOCTYPE html><html lang="en">""" + HTML_BASE_HEAD + """<body class="bg-light">""" + HTML_NAV + """
<div class="container mt-5 mb-5">
    <div class="card shadow mx-auto" style="max-width: 650px;">
        <div class="card-header bg-success text-white"><h4>{{ 'Create' if uid == 'new' else 'Modify' }} User</h4></div>
        <div class="card-body">
            <form method="POST">
                <div class="mb-3"><label class="fw-bold">Name</label><input type="text" name="name" class="form-control" value="{{ user.name if user else '' }}" required></div>
                <div class="mb-3"><label class="fw-bold">Username</label><input type="text" name="username" class="form-control" value="{{ user.username if user else '' }}" required></div>
                <div class="mb-3"><label class="fw-bold">Password</label>
                    {% if uid == 'new' %}<input type="text" name="password" class="form-control" required placeholder="Set initial password">
                    {% elif session['role'] == 'SuperAdmin' or (session['role'] == 'Admin' and user.role != 'SuperAdmin') %}
                        <input type="text" name="password" class="form-control" placeholder="Enter new to change" value="{{ user.password }}">
                    {% else %}<input type="text" class="form-control" value="******" disabled><small class="text-muted d-block mt-1"><i class="bi bi-lock-fill"></i> Only Admins can change passwords.</small>
                    {% endif %}
                </div>
                
                <div class="mb-4">
                    <label class="fw-bold">Role Title (Label Only)</label>
                    <select name="role" class="form-select">
                        <option value="Viewer" {% if user and user.role == 'Viewer' %}selected{% endif %}>Viewer</option>
                        <option value="Editor" {% if user and user.role == 'Editor' %}selected{% endif %}>Editor</option>
                        <option value="Admin" {% if user and user.role == 'Admin' %}selected{% endif %}>Admin</option>
                        {% if session['role'] == 'SuperAdmin' %}<option value="SuperAdmin" {% if user and user.role == 'SuperAdmin' %}selected{% endif %}>SuperAdmin</option>{% endif %}
                    </select>
                </div>
                
                <div class="mb-3 border p-3 bg-light rounded">
                    <h6 class="text-success fw-bold border-bottom pb-2">Custom Feature Access</h6>
                    <div class="table-responsive">
                        <table class="table table-sm text-center align-middle bg-white">
                            <thead>
                                <tr>
                                    <th class="text-start">Module</th>
                                    <th>View</th>
                                    <th>Create</th>
                                    <th>Edit</th>
                                    <th>Delete</th>
                                    <th>Approve</th>
                                    <th>Receive</th>
                                    <th>Purchase</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for mod in ['indent', 'payment', 'gatepass'] %}
                                <tr>
                                    <td class="text-start fw-bold text-capitalize">{{ mod }}</td>
                                    <td><input type="checkbox" name="perm_{{mod}}_view" class="form-check-input" {% if p_dict[mod]['view'] %}checked{% endif %}></td>
                                    <td><input type="checkbox" name="perm_{{mod}}_create" class="form-check-input" {% if p_dict[mod]['create'] %}checked{% endif %}></td>
                                    <td><input type="checkbox" name="perm_{{mod}}_edit" class="form-check-input" {% if p_dict[mod]['edit'] %}checked{% endif %}></td>
                                    <td><input type="checkbox" name="perm_{{mod}}_delete" class="form-check-input" {% if p_dict[mod]['delete'] %}checked{% endif %}></td>
                                    <td><input type="checkbox" name="perm_{{mod}}_approve" class="form-check-input" {% if p_dict[mod]['approve'] %}checked{% endif %}></td>
                                    {% if mod == 'indent' %}
                                        <td><input type="checkbox" name="perm_{{mod}}_mark_received" class="form-check-input border-primary" {% if p_dict[mod].get('mark_received') %}checked{% endif %}></td>
                                        <td><input type="checkbox" name="perm_{{mod}}_mark_purchased" class="form-check-input border-primary" {% if p_dict[mod].get('mark_purchased') %}checked{% endif %}></td>
                                    {% else %}
                                        <td><span class="text-muted">-</span></td><td><span class="text-muted">-</span></td>
                                    {% endif %}
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    <div class="form-check mt-2">
                        <input type="checkbox" name="perm_settings_view" id="permSet" class="form-check-input" {% if p_dict['settings']['view'] %}checked{% endif %}>
                        <label class="form-check-label fw-bold" for="permSet">Access Admin Settings Page</label>
                    </div>
                </div>

                <button type="submit" class="btn btn-success w-100 fw-bold">Save User & Permissions</button>
            </form>
        </div>
    </div>
</div>
</body></html>"""


# ==========================================
# 4. ROUTES
# ==========================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('change_password'): return redirect(url_for('change_password'))
        users = db.collection('users').where('username', '==', request.form['username']).where('password', '==', request.form['password']).stream()
        user = next(users, None)
        if user:
            ud = user.to_dict()
            session.permanent = True
            
            current_fy = get_fy_string(datetime.now())
            if not list(db.collection('financial_years').where('name', '==', current_fy).limit(1).stream()):
                db.collection('financial_years').add({'name': current_fy})
                
            perms = ud.get('permissions')
            if not perms:
                perms = get_default_permissions(ud.get('role', 'Viewer'))
                
            # GUARANTEE SUPERADMIN/ADMIN ALWAYS HAVE SETTINGS ACCESS (Fail-safe)
            if ud.get('role') in ['Admin', 'SuperAdmin']:
                if 'settings' not in perms:
                    perms['settings'] = {}
                perms['settings']['view'] = True
                
            session.update({
                'user_id': user.id, 
                'user_name': ud['name'], 
                'role': ud['role'],
                'permissions': perms,
                'active_fy': current_fy 
            })
            db.collection('login_logs').add({'username': ud['username'], 'name': ud['name'], 'role': ud['role'], 'timestamp': datetime.utcnow()})
            return redirect(url_for('dashboard'))
        flash('Invalid Login')
    return render_template_string(HTML_LOGIN)

@app.route('/switch_fy/<fy>')
def switch_fy(fy):
    if 'user_id' in session:
        session['active_fy'] = fy
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/change_password', methods=['GET', 'POST'])
def change_password():
    if request.method == 'POST':
        username = request.form['username']
        old_pass = request.form['old_password']
        new_pass = request.form['new_password']
        users = db.collection('users').where('username', '==', username).where('password', '==', old_pass).stream()
        user = next(users, None)
        if user:
            db.collection('users').document(user.id).update({'password': new_pass})
            flash('Password Updated Successfully! Please Login.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Invalid Username or Old Password.', 'danger')
    return render_template_string(HTML_CHANGE_PASS)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
def dashboard():
    if 'user_id' not in session: return redirect(url_for('login'))
    if not session.get('permissions', {}).get('indent', {}).get('view', True):
        return redirect(url_for('payment_dashboard'))
        
    page = request.args.get('page', 1, type=int)
    search_query = request.args.get('search', '').strip().lower()
    status_filter = request.args.get('status', 'All') 
    active_fy = session.get('active_fy')
    
    per_page = 40
    indents = []
    
    docs = db.collection('indents').stream() 
    for doc in docs:
        i = doc.to_dict()
        
        doc_fy = i.get('fy')
        if not doc_fy: doc_fy = get_fy_string(i.get('created_at') or datetime.now())
        if doc_fy != active_fy: continue
        
        i['id'] = doc.id
        i['fy'] = doc_fy
        
        if session['role'] == 'Viewer' and i.get('assigned_to') != session['user_name']: continue
        if status_filter == 'Pending' and i.get('received_status') == 'Received': continue
        
        try: i['serial_no'] = int(i.get('serial_no', 0))
        except: i['serial_no'] = 0
        
        if 'department' not in i and 'requester' in i: i['department'] = i['requester']
        i.setdefault('created_by', 'Unknown')
        i.setdefault('created_at', '') 
        i.setdefault('indent_person', '')
        i.setdefault('remarks', '')
        i.setdefault('image_url', '')
        i.setdefault('purchase_status', '')
        i.setdefault('assigned_to', '')
        i.setdefault('department', '')
        
        if search_query:
            combined_text = f"{i.get('item', '')} {i.get('created_by', '')} cr:{i.get('created_by', '')} {i.get('assigned_to', '')} {i.get('indent_person', '')} {i.get('department', '')}".lower()
            if search_query not in combined_text:
                continue
        
        indents.append(i)
        
    indents.sort(key=lambda x: x['serial_no'], reverse=True)
    
    total_items = len(indents)
    start = (page - 1) * per_page
    end = start + per_page
    paginated_indents = indents[start:end]
    has_next = end < total_items
    users = [d.to_dict() for d in db.collection('users').stream()]
    
    return render_template_string(
        HTML_DASHBOARD_INDENT, 
        indents=paginated_indents, 
        session=session, 
        system='indent', 
        today=datetime.today().strftime('%Y-%m-%d'), 
        page=page, 
        has_next=has_next, 
        users=users,
        current_status=status_filter
    )

@app.route('/create', methods=['GET', 'POST'])
def create():
    if not session.get('permissions', {}).get('indent', {}).get('create'): return redirect(url_for('dashboard'))
    active_fy = session.get('active_fy')
    
    if request.method == 'POST':
        images = request.files.getlist('product_image[]')
        
        # STRICT SIZE CHECK BEFORE DB OPERATIONS
        for file in images:
            if file and file.filename != '':
                file.seek(0, os.SEEK_END)
                if file.tell() > 204800:
                    flash(f"ERROR: Image '{file.filename}' exceeds the 200 KB limit. Submission cancelled.", "danger")
                    return redirect(request.referrer)
                file.seek(0)
                
        input_date_str = request.form['indent_date']
        dt_obj = datetime.strptime(input_date_str, '%Y-%m-%d')
        
        if get_fy_string(dt_obj) != active_fy:
            flash(f"ERROR: You entered {input_date_str}, which does not belong to FY {active_fy}. Change the date or switch your active FY.", "danger")
            return redirect(request.referrer)
            
        items = request.form.getlist('item[]')
        reasons = request.form.getlist('reason[]')
        remarks_list = request.form.getlist('remarks[]') 
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        custom_units = request.form.getlist('custom_unit[]')
        
        dept_select = request.form.get('department_select')
        custom_dept = request.form.get('custom_department')
        final_dept = custom_dept.upper() if dept_select == 'Other' and custom_dept else dept_select
        add_if_new('departments', final_dept)

        indent_person = request.form.get('indent_person')
        add_if_new('indent_persons', indent_person)

        existing_units = get_units_list()
        
        manual_start = request.form.get('manual_serial')
        if manual_start and str(manual_start).strip().isdigit() and session.get('permissions', {}).get('indent', {}).get('approve'):
            next_sn = int(manual_start)
            db.collection('counters').document(f"indents_{active_fy}").set({
                'last_value': next_sn + len(items) - 1
            }, merge=True)
        else:
            next_sn = get_next_serial_number('indents', active_fy, count=len(items))
        
        for i in range(len(items)):
            final_unit = units[i]
            if final_unit == 'Other' and custom_units[i]:
                final_unit = custom_units[i].upper()
                if final_unit not in existing_units:
                    db.collection('units').add({'name': final_unit})
                    existing_units.append(final_unit)
                    
            img_data = ""
            if i < len(images) and images[i].filename != '':
                try:
                    file = images[i]
                    file.seek(0)
                    encoded_string = base64.b64encode(file.read()).decode('utf-8')
                    img_data = f"data:{file.content_type};base64,{encoded_string}"
                except Exception as e:
                    print(f"Image Encoding Failed: {e}")
            
            data = {
                'fy': active_fy, 
                'serial_no': next_sn,
                'indent_date': input_date_str, 
                'department': final_dept,
                'indent_person': indent_person, 
                'assigned_to': request.form['assigned_to'], 
                'item': items[i], 
                'reason': reasons[i],
                'remarks': remarks_list[i] if i < len(remarks_list) else "",
                'quantity': int(quantities[i]), 
                'unit': final_unit, 
                'image_url': img_data, 
                'approval_status': 'Pending', 
                'purchase_status': '',
                'received_status': 'Pending', 
                'created_by': session['user_name'],
                'created_at': datetime.now()
            }
            db.collection('indents').add(data)
            next_sn += 1 
            
        flash(f"Entries Saved for FY {active_fy}!", "success")
        return redirect(url_for('dashboard'))
        
    users = [d.to_dict() for d in db.collection('users').stream()]
    return render_template_string(HTML_CREATE_MULTI, users=users, unit_list=get_units_list(), departments=get_departments_list(), persons=get_people_list(), today=datetime.today().strftime('%Y-%m-%d'), session=session, system='indent')

@app.route('/edit/<i_id>', methods=['GET', 'POST'])
def edit_indent(i_id):
    if not session.get('permissions', {}).get('indent', {}).get('edit'): return redirect(url_for('dashboard'))
    doc_ref = db.collection('indents').document(i_id)
    data = doc_ref.get().to_dict()
    doc_fy = data.get('fy') or get_fy_string(data.get('created_at') or datetime.now())
    data['fy'] = doc_fy
    
    if request.method == 'POST':
        input_date_str = request.form['indent_date']
        dt_obj = datetime.strptime(input_date_str, '%Y-%m-%d')
        
        if get_fy_string(dt_obj) != doc_fy:
            flash(f"ERROR: You cannot change the date to a different Financial Year. This document belongs to FY {doc_fy}.", "danger")
            return redirect(request.referrer)
            
        dept = request.form.get('department')
        add_if_new('departments', dept)
        person = request.form.get('indent_person')
        add_if_new('indent_persons', person)

        update_data = {
            'indent_date': input_date_str, 'department': dept, 'indent_person': person,
            'item': request.form['item'], 'reason': request.form['reason'], 'remarks': request.form.get('remarks'),
            'quantity': int(request.form['quantity']), 'unit': request.form['unit'], 'assigned_to': request.form['assigned_to']
        }
        
        if request.form.get('delete_image') == '1':
            update_data['image_url'] = ""

        file = request.files.get('product_image')
        if file and file.filename != '':
            file.seek(0, os.SEEK_END)
            if file.tell() > 204800: 
                flash("ERROR: Uploaded image exceeds the 200 KB limit. Update cancelled.", "danger")
                return redirect(request.referrer)
            file.seek(0)
            encoded_string = base64.b64encode(file.read()).decode('utf-8')
            update_data['image_url'] = f"data:{file.content_type};base64,{encoded_string}"

        if session.get('permissions', {}).get('indent', {}).get('approve') or session['role'] == 'SuperAdmin':
             if 'approval_status' in request.form: 
                 new_status = request.form['approval_status']
                 current_status = data.get('approval_status')
                 
                 if session['role'] != 'SuperAdmin' and new_status in ['Hold', 'Rejected']:
                     flash("Only SuperAdmin can place items on Hold or Reject.", "danger")
                 else:
                     update_data['approval_status'] = new_status
                     
                     if new_status != current_status:
                         if new_status in ['Approved', 'Hold', 'Rejected']:
                             update_data['approved_by_name'] = session['user_name']
                         else:
                             update_data['approved_by_name'] = ""
                     
                     if new_status == 'Rejected':
                         update_data['received_status'] = 'Rejected'
                         update_data['received_date'] = ""

             if 'received_status' in request.form and update_data.get('approval_status') != 'Rejected':
                 if session.get('permissions', {}).get('indent', {}).get('mark_received') or session['role'] == 'SuperAdmin':
                     update_data['received_status'] = request.form['received_status']
                     if request.form['received_status'] == 'Received':
                         update_data['received_date'] = request.form.get('received_date', datetime.today().strftime('%Y-%m-%d'))
                     else:
                         update_data['received_date'] = ""
                     
        doc_ref.update(update_data)
        flash("Indent updated successfully.", "success")
        return redirect(url_for('dashboard'))
    
    users = [d.to_dict() for d in db.collection('users').stream()]
    if 'department' not in data and 'requester' in data: data['department'] = data['requester']
    data.setdefault('indent_person', '')
    data.setdefault('remarks', '')
    return render_template_string(HTML_EDIT, users=users, unit_list=get_units_list(), departments=get_departments_list(), persons=get_people_list(), data=data, session=session, system='indent')

@app.route('/delete/<i_id>')
def delete_indent(i_id):
    if not session.get('permissions', {}).get('indent', {}).get('delete'): return redirect(url_for('dashboard'))
    doc = db.collection('indents').document(i_id).get().to_dict()
    doc_fy = doc.get('fy') or get_fy_string(doc.get('created_at') or datetime.now())
    
    if delete_last_entry_helper('indents', i_id, doc_fy): 
        flash('Last entry deleted successfully.', 'success')
    else: 
        flash('Error: Only last entry of the Financial Year can be deleted.', 'danger')
    return redirect(url_for('dashboard'))

@app.route('/purchase/<i_id>')
def mark_purchased(i_id):
    if not session.get('permissions', {}).get('indent', {}).get('mark_purchased'): return redirect(url_for('dashboard'))
    db.collection('indents').document(i_id).update({
        'purchase_status': 'Purchased',
        'purchased_by': session['user_name'],
        'purchased_at': datetime.now()
    })
    flash("Item marked as Purchased.", "success")
    return redirect(url_for('dashboard'))

@app.route('/reset_purchase/<i_id>')
def reset_purchase(i_id):
    if not session.get('permissions', {}).get('indent', {}).get('mark_purchased'): 
        flash("Unauthorized.", "danger")
        return redirect(url_for('dashboard'))
        
    db.collection('indents').document(i_id).update({
        'purchase_status': '',
        'purchased_by': firestore.DELETE_FIELD,
        'purchased_at': firestore.DELETE_FIELD
    })
    flash("Purchase status has been reset.", "info")
    return redirect(url_for('dashboard'))

@app.route('/bulk_update', methods=['POST'])
def bulk_update():
    ids = request.form.getlist('selected_ids[]')
    action = request.form.get('action')
    if not ids: return redirect(url_for('dashboard'))
    batch = db.batch()
    
    for i_id in ids:
        doc_ref = db.collection('indents').document(i_id)
        if action == 'Received':
            if not session.get('permissions', {}).get('indent', {}).get('mark_received'): continue
            r_date = request.form.get('bulk_received_date')
            batch.update(doc_ref, {'received_status': 'Received', 'received_date': r_date})
        else:
            if action in ['Hold', 'Rejected'] and session.get('role') != 'SuperAdmin':
                continue
            if action == 'Approved' and not session.get('permissions', {}).get('indent', {}).get('approve'):
                continue
                
            update_dict = {'approval_status': action}
            
            if action in ['Approved', 'Hold', 'Rejected']:
                update_dict['approved_by_name'] = request.form.get('approver_name')
            else: 
                update_dict['approved_by_name'] = ""
            
            if action == 'Rejected':
                update_dict['received_status'] = 'Rejected'
                update_dict['received_date'] = ""
                
            batch.update(doc_ref, update_dict)
    batch.commit()
    return redirect(url_for('dashboard'))

@app.route('/reports', methods=['GET', 'POST'])
def reports():
    if 'user_id' not in session: return redirect(url_for('login'))
    active_fy = session.get('active_fy')
    filters = {'start_date': '', 'end_date': '', 'dept_filter': '', 'assigned_filter': 'All', 'status': 'All', 'received_status': 'All', 'sort_by': 'Date'}
    results = []
    users = [d.to_dict() for d in db.collection('users').stream()]
    if request.method == 'POST':
        filters.update({k: request.form.get(k) for k in filters})
        docs = db.collection('indents').stream()
        for doc in docs:
            d = doc.to_dict()
            doc_fy = d.get('fy') or get_fy_string(d.get('created_at') or datetime.now())
            if doc_fy != active_fy: continue
            
            d['id'] = doc.id
            d['fy'] = doc_fy
            try: d['serial_no'] = int(d.get('serial_no', 0))
            except: d['serial_no'] = 0
            if 'department' not in d and 'requester' in d: d['department'] = d['requester']
            d.setdefault('indent_person', '')
            d.setdefault('remarks', '')
            if filters['start_date'] and d['indent_date'] < filters['start_date']: continue
            if filters['end_date'] and d['indent_date'] > filters['end_date']: continue
            if filters['status'] != 'All' and d['approval_status'] != filters['status']: continue
            if filters['dept_filter'] and filters['dept_filter'].lower() not in d['department'].lower(): continue
            if filters['assigned_filter'] != 'All' and d.get('assigned_to') != filters['assigned_filter']: continue
            if filters['received_status'] == 'Received' and d.get('received_status') != 'Received': continue
            if filters['received_status'] == 'Pending' and d.get('received_status') == 'Received': continue
            if filters['received_status'] == 'Rejected' and d.get('received_status') != 'Rejected': continue
            results.append(d)
        if filters['sort_by'] == 'Department': results.sort(key=lambda x: x['department'])
        elif filters['sort_by'] == 'Assigned': results.sort(key=lambda x: x['assigned_to'])
        else: results.sort(key=lambda x: x['serial_no'], reverse=True)
        if request.form.get('action') == 'export':
            if not results: flash("No data", "warning")
            else:
                df = pd.DataFrame(results)[['fy', 'serial_no', 'indent_date', 'department', 'indent_person', 'item', 'quantity', 'unit', 'approval_status', 'approved_by_name', 'received_status', 'assigned_to', 'reason', 'remarks']]
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
                output.seek(0)
                return send_file(output, download_name=f"Indent_Report_FY_{active_fy}.xlsx", as_attachment=True)
    return render_template_string(HTML_REPORTS, session=session, indents=results, filters=filters, users=users, current_time=datetime.now().strftime("%Y-%m-%d"), system='indent')

# --- PAYMENT ROUTES ---
@app.route('/payments')
def payment_dashboard():
    if 'user_id' not in session: return redirect(url_for('login'))
    if not session.get('permissions', {}).get('payment', {}).get('view', True): return redirect(url_for('dashboard'))
    active_fy = session.get('active_fy')
    payments = []
    
    docs = db.collection('payments').stream()
    for doc in docs:
        p = doc.to_dict()
        doc_fy = p.get('fy') or get_fy_string(p.get('created_at') or datetime.now())
        if doc_fy != active_fy: continue
        
        p['id'] = doc.id
        p['fy'] = doc_fy
        try: p['serial_no'] = int(p.get('serial_no', 0))
        except: p['serial_no'] = 0
        if p.get('status') == 'Done': continue
        payments.append(p)
    payments.sort(key=lambda x: x.get('serial_no', 0), reverse=True)
    return render_template_string(HTML_DASHBOARD_PAYMENT, payments=payments, session=session, system='payment')

@app.route('/payments/create', methods=['GET', 'POST'])
def create_payment():
    if not session.get('permissions', {}).get('payment', {}).get('create'): return redirect(url_for('payment_dashboard'))
    active_fy = session.get('active_fy')
    
    if request.method == 'POST':
        entry_type = request.form.get('entry_type', 'Bill')
        
        check_date_str = request.form['bill_date'] if entry_type == 'Bill' else datetime.today().strftime('%Y-%m-%d')
        dt_obj = datetime.strptime(check_date_str, '%Y-%m-%d')
        if get_fy_string(dt_obj) != active_fy:
             flash(f"ERROR: The bill date does not belong to FY {active_fy}.", "danger")
             return redirect(request.referrer)
             
        new_serial = get_next_serial_number('payments', active_fy, count=1)
        data = {
            'fy': active_fy,
            'serial_no': new_serial,
            'status': request.form.get('status', 'Pending'),
            'approved_by': request.form['approved_by'],
            'created_at': datetime.now(),
            'type': entry_type,
            'payment_date': '', 'payment_mode': '', 'transaction_ref': ''
        }
        
        if entry_type == 'Bill':
            data.update({
                'party_name': request.form['party_name'],
                'bill_number': request.form['bill_number'],
                'bill_date': request.form['bill_date'],
                'due_date': request.form['due_date'],
                'amount': request.form['amount']
            })
        else:
            bank_details = ""
            if request.form['payment_type'] == 'Advance':
                bank_details = f"{request.form['bank_name']}, Br:{request.form['branch_name']}, Acc:{request.form['account_no']}, IFSC:{request.form['ifsc']}"
            
            data.update({
                'party_name': request.form['adv_party_name'],
                'quotation_no': request.form['quotation_no'],
                'item_detail': request.form['item_detail'],
                'qty': request.form['qty'],
                'price': request.form['price'],
                'tax': request.form['tax'],
                'freight': request.form['freight'],
                'amount': request.form['adv_amount'],
                'payment_type': request.form['payment_type'],
                'delivery_time': request.form['delivery_time'],
                'bank_details': bank_details
            })

        db.collection('payments').add(data)
        flash("Payment entry saved.", "success")
        return redirect(url_for('payment_dashboard'))
    return render_template_string(HTML_CREATE_PAYMENT, today=datetime.today().strftime('%Y-%m-%d'), session=session, system='payment')

@app.route('/payments/edit/<p_id>', methods=['GET', 'POST'])
def edit_payment(p_id):
    if not session.get('permissions', {}).get('payment', {}).get('edit'): return redirect(url_for('payment_dashboard'))
    doc_ref = db.collection('payments').document(p_id)
    data = doc_ref.get().to_dict()
    doc_fy = data.get('fy') or get_fy_string(data.get('created_at') or datetime.now())
    data['fy'] = doc_fy
    
    if request.method == 'POST':
        if 'bill_date' in request.form:
             dt_obj = datetime.strptime(request.form['bill_date'], '%Y-%m-%d')
             if get_fy_string(dt_obj) != doc_fy:
                  flash(f"ERROR: Cannot move entry to a different FY. Must stay in {doc_fy}.", "danger")
                  return redirect(request.referrer)
                  
        update_data = {
            'party_name': request.form['party_name'], 
            'amount': request.form['amount'], 
            'approved_by': request.form['approved_by'], 
            'status': request.form['status']
        }
        if 'bill_number' in request.form: update_data['bill_number'] = request.form['bill_number']
        if 'bill_date' in request.form: update_data['bill_date'] = request.form['bill_date']
        if 'due_date' in request.form: update_data['due_date'] = request.form['due_date']
        if 'quotation_no' in request.form: update_data['quotation_no'] = request.form['quotation_no']
        if 'item_detail' in request.form: update_data['item_detail'] = request.form['item_detail']
        if 'delivery_time' in request.form: update_data['delivery_time'] = request.form['delivery_time']
        
        if request.form['status'] == 'Done':
            update_data.update({'payment_date': request.form.get('payment_date'), 'payment_mode': request.form.get('payment_mode'), 'transaction_ref': request.form.get('transaction_ref')})
        doc_ref.update(update_data)
        return redirect(url_for('payment_dashboard'))
    return render_template_string(HTML_EDIT_PAYMENT, data=data, session=session, system='payment')

@app.route('/payments/delete/<p_id>')
def delete_payment(p_id):
    if not session.get('permissions', {}).get('payment', {}).get('delete'): return redirect(url_for('payment_dashboard'))
    doc = db.collection('payments').document(p_id).get().to_dict()
    doc_fy = doc.get('fy') or get_fy_string(doc.get('created_at') or datetime.now())
    
    if delete_last_entry_helper('payments', p_id, doc_fy): 
        flash('Last payment entry deleted successfully.', 'success')
    else: 
        flash('Error: Only last payment entry of the Financial Year can be deleted.', 'danger')
    return redirect(url_for('payment_dashboard'))

@app.route('/payment_reports', methods=['GET', 'POST'])
def payment_reports():
    if 'user_id' not in session: return redirect(url_for('login'))
    active_fy = session.get('active_fy')
    filters = {'start_date': '', 'end_date': '', 'party_filter': '', 'status': 'All', 'sort_by': 'Party'}
    results = []
    if request.method == 'POST':
        filters.update({k: request.form.get(k) for k in filters})
        docs = db.collection('payments').stream()
        for doc in docs:
            p = doc.to_dict()
            doc_fy = p.get('fy') or get_fy_string(p.get('created_at') or datetime.now())
            if doc_fy != active_fy: continue
            
            p['id'] = doc.id
            p['fy'] = doc_fy
            try: p['serial_no'] = int(p.get('serial_no', 0))
            except: p['serial_no'] = 0
            
            check_date = p.get('bill_date') or p.get('created_at').strftime('%Y-%m-%d')
            if filters['start_date'] and check_date < filters['start_date']: continue
            if filters['end_date'] and check_date > filters['end_date']: continue
            if filters['status'] != 'All' and p['status'] != filters['status']: continue
            if filters['party_filter'] and filters['party_filter'].lower() not in p['party_name'].lower(): continue
            results.append(p)
        
        if filters['sort_by'] == 'Party': results.sort(key=lambda x: x.get('party_name', ''))
        else: results.sort(key=lambda x: x.get('serial_no', 0), reverse=True)
        
        if request.form.get('action') == 'export':
            if not results: flash("No data", "warning")
            else:
                bills, advances = [], []
                for r in results:
                    base = {'FY': r['fy'], 'Serial': r['serial_no'], 'Party Name': r['party_name'], 'Amount': r['amount'], 'Status': r['status'], 'Approved By': r['approved_by'], 'Paid Date': r.get('payment_date', ''), 'Paid Mode': r.get('payment_mode', ''), 'Trans Ref': r.get('transaction_ref', '')}
                    if r.get('type') == 'Advance':
                        adv_row = base.copy()
                        adv_row.update({'Quotation No': r.get('quotation_no', ''), 'Item Detail': r.get('item_detail', ''), 'Qty': r.get('qty', ''), 'Price': r.get('price', ''), 'Tax': r.get('tax', ''), 'Freight': r.get('freight', ''), 'Payment Type': r.get('payment_type', ''), 'Delivery Time': r.get('delivery_time', ''), 'Bank Details': r.get('bank_details', '')})
                        advances.append(adv_row)
                    else:
                        bill_row = base.copy()
                        bill_row.update({'Bill Number': r.get('bill_number', ''), 'Bill Date': r.get('bill_date', ''), 'Due Date': r.get('due_date', '')})
                        bills.append(bill_row)

                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    pd.DataFrame(bills).to_excel(writer, sheet_name='Regular Bills', index=False) if bills else pd.DataFrame([{'Info': 'No Bills'}]).to_excel(writer, sheet_name='Regular Bills', index=False)
                    pd.DataFrame(advances).to_excel(writer, sheet_name='Advance Orders', index=False) if advances else pd.DataFrame([{'Info': 'No Advances'}]).to_excel(writer, sheet_name='Advance Orders', index=False)
                output.seek(0)
                return send_file(output, download_name=f"Payment_Report_FY_{active_fy}.xlsx", as_attachment=True)
                
    return render_template_string(HTML_REPORTS_PAYMENT, session=session, payments=results, filters=filters, current_time=datetime.now().strftime("%Y-%m-%d"), system='payment')

# --- GATE PASS ROUTES ---
@app.route('/gatepass')
def gatepass_dashboard():
    if 'user_id' not in session: return redirect(url_for('login'))
    if not session.get('permissions', {}).get('gatepass', {}).get('view', True): return redirect(url_for('dashboard'))
    active_fy = session.get('active_fy')
    search_query = request.args.get('search', '').strip().lower()
    
    gatepasses = []
    docs = db.collection('gatepasses').where('fy', '==', active_fy).stream()
    
    for doc in docs:
        gp = doc.to_dict()
        gp['id'] = doc.id
        try: gp['serial_no'] = int(gp.get('serial_no', 0))
        except: gp['serial_no'] = 0
        
        if search_query:
            comb = f"{gp.get('company_name','')} {gp.get('product','')} {gp.get('by_hand_person','')} {gp.get('purpose','')} {gp.get('reason','')} {gp.get('remark','')}".lower()
            if search_query not in comb:
                continue
                
        gatepasses.append(gp)
        
    gatepasses.sort(key=lambda x: x.get('serial_no', 0), reverse=True)
    return render_template_string(HTML_DASHBOARD_GATEPASS, gatepasses=gatepasses, session=session, system='gatepass')

@app.route('/gatepass/create', methods=['GET', 'POST'])
def create_gatepass():
    if not session.get('permissions', {}).get('gatepass', {}).get('create'): return redirect(url_for('gatepass_dashboard'))
    active_fy = session.get('active_fy')
    
    if request.method == 'POST':
        out_date_str = request.form['out_date']
        dt_obj = datetime.strptime(out_date_str, '%Y-%m-%d')
        if get_fy_string(dt_obj) != active_fy:
             flash(f"ERROR: The date does not belong to FY {active_fy}.", "danger")
             return redirect(request.referrer)
             
        company_select = request.form.get('company_select')
        custom_company = request.form.get('custom_company')
        final_company = custom_company.upper() if company_select == 'Other' and custom_company else company_select
        add_if_new('companies', final_company)
             
        new_serial = get_next_serial_number('gatepasses', active_fy, count=1)
        data = {
            'fy': active_fy,
            'serial_no': new_serial,
            'created_at': datetime.now(),
            'created_by': session['user_name'],
            'type': request.form['gp_type'],
            'out_date': out_date_str,
            'company_name': final_company,
            'product': request.form['product'],
            'qty': request.form['qty'],
            'by_hand_person': request.form['by_hand_person'],
            'purpose': request.form.get('purpose', ''),
            'reason': request.form.get('reason', ''),
            'remark': request.form.get('remark', ''),
            'status': 'Pending',
            'clear_date': '',
            'clear_by': ''
        }

        db.collection('gatepasses').add(data)
        flash("Gate Pass entry saved.", "success")
        return redirect(url_for('gatepass_dashboard'))
        
    companies = get_companies_list()
    users = [d.to_dict() for d in db.collection('users').stream()]
    return render_template_string(HTML_CREATE_GATEPASS, today=datetime.today().strftime('%Y-%m-%d'), session=session, system='gatepass', companies=companies, users=users)

@app.route('/gatepass/edit/<gp_id>', methods=['GET', 'POST'])
def edit_gatepass(gp_id):
    if not session.get('permissions', {}).get('gatepass', {}).get('edit'): return redirect(url_for('gatepass_dashboard'))
    doc_ref = db.collection('gatepasses').document(gp_id)
    data = doc_ref.get().to_dict()
    doc_fy = data.get('fy') or get_fy_string(data.get('created_at') or datetime.now())
    data['fy'] = doc_fy
    
    if request.method == 'POST':
        out_date_str = request.form['out_date']
        dt_obj = datetime.strptime(out_date_str, '%Y-%m-%d')
        if get_fy_string(dt_obj) != doc_fy:
             flash(f"ERROR: Cannot move entry to a different FY. Must stay in {doc_fy}.", "danger")
             return redirect(request.referrer)
             
        company_name = request.form.get('company_name', '').upper()
        add_if_new('companies', company_name)
             
        update_data = {
            'type': request.form['gp_type'],
            'out_date': out_date_str,
            'company_name': company_name,
            'product': request.form['product'],
            'qty': request.form['qty'],
            'by_hand_person': request.form['by_hand_person'],
            'purpose': request.form.get('purpose', ''),
            'reason': request.form.get('reason', ''),
            'remark': request.form.get('remark', ''),
            'status': request.form['status']
        }
        
        if request.form['status'] == 'Cleared':
            update_data['clear_date'] = request.form.get('clear_date', datetime.today().strftime('%Y-%m-%d'))
            update_data['clear_by'] = request.form.get('clear_by', '')
        else:
            update_data['clear_date'] = ''
            update_data['clear_by'] = ''
            
        doc_ref.update(update_data)
        flash("Gate Pass updated successfully.", "success")
        return redirect(url_for('gatepass_dashboard'))
        
    companies = get_companies_list()
    users = [d.to_dict() for d in db.collection('users').stream()]
    return render_template_string(HTML_EDIT_GATEPASS, data=data, session=session, system='gatepass', companies=companies, users=users)

@app.route('/gatepass/delete/<gp_id>')
def delete_gatepass(gp_id):
    if not session.get('permissions', {}).get('gatepass', {}).get('delete'): return redirect(url_for('gatepass_dashboard'))
    doc = db.collection('gatepasses').document(gp_id).get().to_dict()
    doc_fy = doc.get('fy') or get_fy_string(doc.get('created_at') or datetime.now())
    
    if delete_last_entry_helper('gatepasses', gp_id, doc_fy): 
        flash('Last Gate Pass entry deleted successfully.', 'success')
    else: 
        flash('Error: Only the last entry of the Financial Year can be deleted.', 'danger')
    return redirect(url_for('gatepass_dashboard'))

@app.route('/gatepass_reports', methods=['GET', 'POST'])
def gatepass_reports():
    if 'user_id' not in session: return redirect(url_for('login'))
    active_fy = session.get('active_fy')
    filters = {'start_date': '', 'end_date': '', 'gp_type': 'All', 'status': 'All', 'search_filter': ''}
    results = []
    
    if request.method == 'POST':
        filters.update({k: request.form.get(k, '') for k in filters})
        docs = db.collection('gatepasses').where('fy', '==', active_fy).stream()
        
        for doc in docs:
            gp = doc.to_dict()
            gp['id'] = doc.id
            try: gp['serial_no'] = int(gp.get('serial_no', 0))
            except: gp['serial_no'] = 0
            
            check_date = gp.get('out_date') or gp.get('created_at').strftime('%Y-%m-%d')
            if filters['start_date'] and check_date < filters['start_date']: continue
            if filters['end_date'] and check_date > filters['end_date']: continue
            if filters['gp_type'] != 'All' and gp.get('type') != filters['gp_type']: continue
            if filters['status'] != 'All' and gp.get('status') != filters['status']: continue
            
            if filters['search_filter']:
                s_lower = filters['search_filter'].lower()
                if s_lower not in gp.get('company_name', '').lower() and s_lower not in gp.get('by_hand_person', '').lower():
                    continue
                    
            results.append(gp)
            
        results.sort(key=lambda x: x.get('serial_no', 0), reverse=True)
        
        if request.form.get('action') == 'export':
            if not results: flash("No data", "warning")
            else:
                export_data = []
                for r in results:
                    export_data.append({
                        'FY': r.get('fy'),
                        'Serial No': r.get('serial_no'),
                        'Type': r.get('type'),
                        'Out Date': r.get('out_date'),
                        'Company Name': r.get('company_name'),
                        'Product': r.get('product'),
                        'Qty': r.get('qty'),
                        'By Hand Person': r.get('by_hand_person'),
                        'Purpose': r.get('purpose'),
                        'Reason': r.get('reason'),
                        'Remark': r.get('remark'),
                        'Status': r.get('status'),
                        'Clear Date': r.get('clear_date', ''),
                        'Clear By': r.get('clear_by', '')
                    })
                df = pd.DataFrame(export_data)
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False)
                output.seek(0)
                return send_file(output, download_name=f"GatePass_Report_FY_{active_fy}.xlsx", as_attachment=True)
                
    return render_template_string(HTML_REPORTS_GATEPASS, session=session, gatepasses=results, filters=filters, current_time=datetime.now().strftime("%Y-%m-%d"), system='gatepass')

# --- SETTINGS, USERS & MAINTENANCE ---
@app.route('/settings')
def settings():
    if session.get('role') not in ['Admin', 'SuperAdmin'] and not session.get('permissions', {}).get('settings', {}).get('view'): 
        return redirect(url_for('dashboard'))
    units = [dict(id=d.id, **d.to_dict()) for d in db.collection('units').stream()]
    companies = [dict(id=d.id, **d.to_dict()) for d in db.collection('companies').stream()]
    users = [dict(id=d.id, **d.to_dict()) for d in db.collection('users').stream()]
    logs = [dict(id=d.id, **d.to_dict()) for d in db.collection('login_logs').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(50).stream()] if session.get('role') == 'SuperAdmin' else []
    return render_template_string(HTML_SETTINGS, session=session, units=units, companies=companies, users=users, logs=logs, system='indent')

@app.route('/settings/add_fy', methods=['POST'])
def add_fy():
    if session.get('role') not in ['Admin', 'SuperAdmin'] and not session.get('permissions', {}).get('settings', {}).get('view'): return redirect(url_for('dashboard'))
    fy_name = request.form['fy_name'].strip()
    if not list(db.collection('financial_years').where('name', '==', fy_name).stream()): 
        db.collection('financial_years').add({'name': fy_name})
        flash(f"Financial Year {fy_name} Created Successfully", "success")
    else:
        flash(f"Financial Year {fy_name} already exists", "warning")
    return redirect(url_for('settings'))

@app.route('/settings/delete_fy/<fy_name>')
def delete_fy(fy_name):
    if session.get('role') not in ['Admin', 'SuperAdmin'] and not session.get('permissions', {}).get('settings', {}).get('view'): return redirect(url_for('dashboard'))
    docs = db.collection('financial_years').where('name', '==', fy_name).stream()
    for doc in docs:
        db.collection('financial_years').document(doc.id).delete()
    flash(f"Financial Year {fy_name} removed from list.", "info")
    return redirect(url_for('settings'))

@app.route('/settings/add_unit', methods=['POST'])
def add_unit():
    if session.get('role') not in ['Admin', 'SuperAdmin'] and not session.get('permissions', {}).get('settings', {}).get('view'): return redirect(url_for('dashboard'))
    unit_name = request.form['unit_name'].upper()
    if not list(db.collection('units').where('name', '==', unit_name).stream()): db.collection('units').add({'name': unit_name})
    return redirect(url_for('settings'))

@app.route('/settings/delete_unit/<uid>')
def delete_unit(uid):
    if session.get('role') not in ['Admin', 'SuperAdmin'] and not session.get('permissions', {}).get('settings', {}).get('view'): return redirect(url_for('dashboard'))
    db.collection('units').document(uid).delete()
    return redirect(url_for('settings'))

@app.route('/settings/add_company', methods=['POST'])
def add_company():
    if session.get('role') not in ['Admin', 'SuperAdmin'] and not session.get('permissions', {}).get('settings', {}).get('view'): return redirect(url_for('dashboard'))
    company_name = request.form['company_name'].upper()
    if not list(db.collection('companies').where('name', '==', company_name).stream()): db.collection('companies').add({'name': company_name})
    return redirect(url_for('settings'))

@app.route('/settings/delete_company/<cid>')
def delete_company(cid):
    if session.get('role') not in ['Admin', 'SuperAdmin'] and not session.get('permissions', {}).get('settings', {}).get('view'): return redirect(url_for('dashboard'))
    db.collection('companies').document(cid).delete()
    return redirect(url_for('settings'))

@app.route('/users/edit/<uid>', methods=['GET', 'POST'])
def edit_user(uid):
    if session.get('role') not in ['Admin', 'SuperAdmin']: 
        return redirect(url_for('dashboard'))
        
    user_data = None if uid == 'new' else db.collection('users').document(uid).get().to_dict()
    
    # Prevent Admins from editing SuperAdmin users
    if session['role'] == 'Admin' and user_data and user_data.get('role') == 'SuperAdmin':
        flash("Admins cannot edit SuperAdmin profiles.", "danger")
        return redirect(url_for('settings'))
    
    current_role = user_data.get('role', 'Viewer') if user_data else 'Viewer'
    p_dict = user_data.get('permissions') if user_data and 'permissions' in user_data else get_default_permissions(current_role)

    if request.method == 'POST':
        # Ensure Admin cannot create a new SuperAdmin
        submitted_role = request.form['role']
        if session['role'] == 'Admin' and submitted_role == 'SuperAdmin':
            submitted_role = 'Admin' 
            
        data = {'name': request.form['name'], 'username': request.form['username'], 'role': submitted_role}
        pwd = request.form.get('password')
        
        permissions = {'settings': {'view': 'perm_settings_view' in request.form}}
        for mod in ['indent', 'payment', 'gatepass']:
            permissions[mod] = {
                'view': f'perm_{mod}_view' in request.form,
                'create': f'perm_{mod}_create' in request.form,
                'edit': f'perm_{mod}_edit' in request.form,
                'delete': f'perm_{mod}_delete' in request.form,
                'approve': f'perm_{mod}_approve' in request.form
            }
            if mod == 'indent':
                permissions[mod]['mark_received'] = 'perm_indent_mark_received' in request.form
                permissions[mod]['mark_purchased'] = 'perm_indent_mark_purchased' in request.form
                
        data['permissions'] = permissions

        if uid == 'new':
            data['password'] = pwd
            db.collection('users').add(data)
        else:
            if session['role'] == 'SuperAdmin' and pwd: 
                data['password'] = pwd
            # Admin can change password of lower roles
            if session['role'] == 'Admin' and pwd and current_role != 'SuperAdmin':
                data['password'] = pwd
                
            db.collection('users').document(uid).update(data)
        return redirect(url_for('settings'))
        
    return render_template_string(HTML_EDIT_USER, uid=uid, user=user_data, p_dict=p_dict, session=session, system='indent')

@app.route('/users/delete/<uid>')
def delete_user(uid):
    if session.get('role') not in ['Admin', 'SuperAdmin'] and not session.get('permissions', {}).get('settings', {}).get('view'): return redirect(url_for('dashboard'))
    target_user_ref = db.collection('users').document(uid)
    target_user = target_user_ref.get().to_dict()
    if session['role'] == 'Admin' and target_user.get('role') == 'SuperAdmin':
        flash("Admins cannot delete SuperAdmins.", "danger")
    elif uid == session['user_id']:
        flash("You cannot delete yourself.", "warning")
    else:
        target_user_ref.delete()
    return redirect(url_for('settings'))

@app.route('/settings/fix_serials', methods=['POST'])
def fix_serials():
    if session.get('role') not in ['Admin', 'SuperAdmin']:
        flash("Unauthorized", "danger")
        return redirect(url_for('settings'))

    collection_name = request.form.get('collection_name')
    fy_name = request.form.get('fy_name')

    docs = db.collection(collection_name).stream()
    doc_list = []
    
    for doc in docs:
        d = doc.to_dict()
        doc_fy = d.get('fy') or get_fy_string(d.get('created_at') or datetime.now())
        
        if doc_fy == fy_name:
            d['id'] = doc.id
            if collection_name == 'indents':
                sort_date_str = str(d.get('indent_date', ''))
            elif collection_name == 'gatepasses':
                sort_date_str = str(d.get('out_date', ''))
            else:
                sort_date_str = str(d.get('bill_date', d.get('payment_date', '')))
            
            if not sort_date_str or sort_date_str == 'None':
                c_at = d.get('created_at')
                if isinstance(c_at, datetime):
                    sort_date_str = c_at.strftime('%Y-%m-%d')
                else:
                    sort_date_str = '1970-01-01'
                    
            c_at = d.get('created_at')
            if not isinstance(c_at, datetime):
                c_at = datetime.min
            c_at = c_at.replace(tzinfo=None) 

            d['sort_date'] = sort_date_str
            d['sort_time'] = c_at
            
            doc_list.append(d)

    if not doc_list:
        flash(f"No records found in '{collection_name}' for FY '{fy_name}'.", "warning")
        return redirect(url_for('settings'))

    doc_list.sort(key=lambda x: (x['sort_date'], x['sort_time']))

    batch = db.batch()
    new_serial = 1
    updates_made = 0

    for item in doc_list:
        doc_ref = db.collection(collection_name).document(item['id'])
        batch.update(doc_ref, {'serial_no': new_serial, 'fy': fy_name})
        
        new_serial += 1
        updates_made += 1
        
        if updates_made % 400 == 0:
            batch.commit()
            batch = db.batch()

    if updates_made % 400 != 0:
        batch.commit()

    db.collection('counters').document(f"{collection_name}_{fy_name}").set({
        'last_value': updates_made
    })

    flash(f"Successfully sorted by Date and fixed {updates_made} serial numbers for {collection_name} ({fy_name}).", "success")
    return redirect(url_for('settings'))

@app.route('/settings/backup')
def backup_database():
    if session.get('role') != 'SuperAdmin': return "Unauthorized", 403
    collections = ['users', 'units', 'departments', 'financial_years', 'indent_persons', 'indents', 'payments', 'gatepasses', 'counters', 'login_logs', 'companies']
    backup_data = {}
    
    for coll in collections:
        backup_data[coll] = {}
        for doc in db.collection(coll).stream():
            backup_data[coll][doc.id] = doc.to_dict()
            
    output = io.BytesIO()
    output.write(json.dumps(backup_data, cls=FirestoreEncoder).encode('utf-8'))
    output.seek(0)
    
    filename = f"DPPL_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return send_file(output, download_name=filename, as_attachment=True, mimetype='application/json')

@app.route('/settings/restore', methods=['POST'])
def restore_database():
    if session.get('role') != 'SuperAdmin': return "Unauthorized", 403
    
    file = request.files.get('backup_file')
    if not file or not file.filename.endswith('.json'):
        flash("Invalid file format. Please upload a JSON backup.", "danger")
        return redirect(url_for('settings'))
    
    try:
        data = json.loads(file.read().decode('utf-8'), object_hook=firestore_decoder)
        batch = db.batch()
        count = 0
        
        for coll_name, docs in data.items():
            for doc_id, doc_data in docs.items():
                doc_ref = db.collection(coll_name).document(doc_id)
                batch.set(doc_ref, doc_data)
                count += 1
                
                if count % 400 == 0:
                    batch.commit()
                    batch = db.batch()
                    
        batch.commit()
        flash(f"Database restored successfully! Processed {count} records across all collections.", "success")
    except Exception as e:
        flash(f"Restore failed: {str(e)}", "danger")
        
    return redirect(url_for('settings'))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
