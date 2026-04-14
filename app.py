from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from firebase_config import db
from firebase_admin import firestore
from geopy.distance import geodesic
import hashlib
import qrcode
import io
import base64
import secrets # Used for dynamic tokens

app = Flask(__name__)
app.secret_key = 'super_secret_attendance_key' 

# --- HELPER FUNCTIONS ---
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('login.html')

# NEW: Route to show the Professor Registration Page
@app.route('/register-prof')
def register_prof_page():
    return render_template('register_prof.html')

@app.route('/register-check', methods=['POST'])
def register_check():
    """The Gatekeeper: Decides which registration form to show"""
    code = request.form.get('reg_code')
    
    # 1. If they type the Professor Code
    if code == "ADMIN2026":
        return render_template('register_prof.html')
    
    # 2. If they type the Student Code
    elif code == "DEV123":
        return render_template('register.html')
    
    # 3. If they type anything else
    else:
        flash("Invalid Registration Code! Please contact your professor.")
        return redirect(url_for('index'))
    

@app.route('/api/register', methods=['POST'])
def handle_registration():
    try:
        # Capture form data
        email = request.form.get('email')
        # ... other fields ...

        # --- THE FIX IS HERE ---
        # 1. Get the code and remove any accidental spaces with .strip()
        prof_code = request.form.get('prof_admin_code', '').strip()
        
        # 2. Logic: If it matches ADMIN2026, role is 1, else 0
        role_flag = 1 if prof_code == "ADMIN2026" else 0
        
        # Debug: This will show in your terminal exactly what was detected
        print(f"DEBUG: Entered Code: '{prof_code}' | Resulting Role: {role_flag}")

        user_ref = db.collection('users').document(email)
        user_ref.set({
            'name': request.form.get('name'),
            'email': email,
            'enrollment_no': request.form.get('enrollment'),
            'branch': request.form.get('branch'),
            'password': hash_password(request.form.get('password')),
            'is_admin': role_flag,  # This will now be 1 if code matched
            'proxy_flag': False
        })
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Registration Error: {e}")
        return redirect(url_for('index'))   

@app.route('/login', methods=['POST'])
def login():
    session.clear() # Clear old session data
    email = request.form.get('email')
    password = hash_password(request.form.get('password'))
    
    user_doc = db.collection('users').document(email).get()
    
    if user_doc.exists:
        user_data = user_doc.to_dict()
        if user_data['password'] == password:
            session['user'] = user_data
            
            # Use string comparison to be extra safe
            is_admin = str(user_data.get('is_admin', '0'))
            
            if is_admin == '1':
                return redirect(url_for('professor_dashboard'))
            else:
                return redirect(url_for('student_dashboard'))
        else:
            flash("Incorrect Password!")
            return redirect(url_for('index')) # Return 1
    else:
        flash("User not found!")
        return redirect(url_for('index')) # Return 2

    # Fallback return just in case everything else fails
    return redirect(url_for('index')) # Return 3
    

@app.route('/student-dashboard')
def student_dashboard():
    if 'user' not in session: return redirect(url_for('index'))
    if str(session['user'].get('is_admin')) == '1':
        return redirect(url_for('professor_dashboard'))
    email = session['user']['email']
    branch = session['user']['branch']

    assn_docs = db.collection('assignments').where('branch', '==', branch).stream()
    assignments = [doc.to_dict() for doc in assn_docs]

    marks_doc = db.collection('marks').document(email).get()
    marks_data = marks_doc.to_dict() if marks_doc.exists else {"Mid Term": "--", "End Sem": "--", "Practical": "--", "Total": "0/115"}

    att_docs = db.collection('attendance').where('email', '==', email).stream()
    attendance_count = len(list(att_docs)) + 4 
    width = (attendance_count / 8) * 100

    return render_template('student_db.html', user=session['user'], assignments=assignments, marks=marks_data, attendance_count=attendance_count, width=width)

@app.route('/professor-dashboard')
def professor_dashboard():
    if 'user' not in session or session['user'].get('is_admin') != 1:
        return redirect(url_for('index'))
    return render_template('admin_db.html', user=session['user'])

# NEW: Professor Start Attendance Route (Sets the token and location)
@app.route('/professor/start-attendance', methods=['POST'])
def start_attendance():
    branch_id = request.form.get('branch_id')
    lat = float(request.form.get('lat'))
    lng = float(request.form.get('lng'))
    
    # Generate a random token for this session
    session_token = secrets.token_hex(4) 
    
    db.collection('branches').document(branch_id).update({
        'attendance_active': True,
        'current_token': session_token,
        'lat': lat,
        'lng': lng,
        'last_started': firestore.SERVER_TIMESTAMP
    })
    return {"status": "success", "token": session_token}

@app.route('/submit-attendance', methods=['POST'])
def submit_attendance():
    if 'user' not in session: return "Unauthorized", 401
    try:
        student_lat = float(request.form.get('lat'))
        student_lng = float(request.form.get('lng'))
        branch_id = session['user']['branch']
        provided_token = request.form.get('token') # Token from the QR URL

        branch_ref = db.collection('branches').document(branch_id).get()
        branch_data = branch_ref.to_dict()

        # SECURITY CHECK 1: Is session active?
        if not branch_data.get('attendance_active'):
            return "Error: Attendance session is closed!", 403

        # SECURITY CHECK 2: Does the token match the current session?
        if branch_data.get('current_token') != provided_token:
            return "Error: Invalid or Expired QR Code!", 403

        # SECURITY CHECK 3: Geofencing
        prof_coords = (branch_data['lat'], branch_data['lng'])
        student_coords = (student_lat, student_lng)
        distance = geodesic(student_coords, prof_coords).meters

        if distance <= 30:
            db.collection('attendance').add({
                'email': session['user']['email'],
                'enrollment': session['user']['enrollment_no'],
                'branch': branch_id,
                'timestamp': firestore.SERVER_TIMESTAMP,
                'proxy_flag': False
            })
            return "Success! Attendance marked.", 200
        return f"Too far! You are {round(distance)}m away.", 403
    except Exception as e:
        return f"Server Error: {e}", 500

@app.route('/generate_qr/<branch_id>')
def generate_qr(branch_id):
    if 'user' not in session or session['user'].get('is_admin') != 1:
        return "Unauthorized", 403

    # Fetch branch data
    branch_ref = db.collection('branches').document(branch_id).get()
    
    # --- SAFETY CHECK ---
    if not branch_ref.exists:
        # If branch doesn't exist, we create it with default values
        db.collection('branches').document(branch_id).set({
            'attendance_active': False,
            'current_token': 'none',
            'lat': 0.0,
            'lng': 0.0
        })
        token = 'none'
    else:
        branch_data = branch_ref.to_dict()
        token = branch_data.get('current_token', 'none')

    # Generate QR with token
    qr_data = f"http://192.168.29.16:5000/mark-attendance-page/{branch_id}?token={token}"
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="purple", back_color="white")
    
    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    
    # CRITICAL: Add the prefix here so the <img> tag understands it
    img_base64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    return f"data:image/png;base64,{img_base64}"


@app.route('/mark-attendance-page/<branch_id>')
def mark_attendance_page(branch_id):
    token = request.args.get('token') # Capture token from URL
    return render_template('mark_att.html', branch_id=branch_id, token=token)

# 1. The Page that shows the QR + Live List
@app.route('/professor/live-session/<branch_id>')
def live_session(branch_id):
    if 'user' not in session or session['user'].get('is_admin') != 1:
        return redirect(url_for('index'))
    return render_template('live_session.html', branch_id=branch_id)

# 2. API to get students for a specific branch
@app.route('/api/get-branch-students/<branch_id>')
def get_branch_students(branch_id):
    # Get all students registered in this branch
    students = db.collection('users').where('branch', '==', branch_id).stream()
    
    # Get everyone who marked attendance TODAY
    # (Assuming you have a 'date' field or just checking recent timestamps)
    attendance = db.collection('attendance').where('branch', '==', branch_id).stream()
    present_emails = [doc.to_dict()['email'] for doc in attendance]

    student_list = []
    for s in students:
        data = s.to_dict()
        data['is_present'] = data['email'] in present_emails
        student_list.append(data)
    
    return jsonify(student_list)

# 3. API to toggle Proxy Flag
@app.route('/api/toggle-proxy', methods=['POST'])
def toggle_proxy():
    email = request.form.get('email')
    status = request.form.get('status') == 'true'
    db.collection('users').document(email).update({'proxy_flag': status})
    return jsonify({"success": True})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)