from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from firebase_config import db
from firebase_admin import firestore
from geopy.distance import geodesic
import hashlib
import qrcode
import io
import base64
import secrets # Used for dynamic tokens
from datetime import datetime

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
    code = request.form.get('reg_code')
    
    # 1. If they are a Professor
    if code == "ADMIN2026":
        return render_template('register_prof.html')
    
    # 2. If they are a Student
    elif code == "DEV123":
        try:
            # FETCH BRANCHES FROM DATABASE
            # This looks at your root 'branches' collection
            branches_ref = db.collection('branches').stream()
            branches_list = [doc.id for doc in branches_ref]
            
            # Debug: Check your terminal to see if branches are found
            print(f"DEBUG: Found branches for registration: {branches_list}")
            
            # Pass the list to the template
            return render_template('register.html', branches=branches_list)
            
        except Exception as e:
            print(f"Error loading branches: {e}")
            # Fallback to an empty list so the page still loads
            return render_template('register.html', branches=[])
    
    # 3. Invalid Code
    else:
        flash("Invalid Registration Code!")
        return redirect(url_for('index'))
    

@app.route('/api/register', methods=['POST'])
def handle_registration():
    try:
        # 1. Capture Form Data
        email = request.form.get('email')
        name = request.form.get('name')
        enrollment = request.form.get('enrollment')
        branch = request.form.get('branch', '').strip().upper() # e.g., "IT2"
        password = hash_password(request.form.get('password'))
        
        # Determine if Admin based on your code
        prof_code = request.form.get('prof_admin_code', '').strip()
        role_flag = 1 if prof_code == "ADMIN2026" else 0

        user_data = {
            'name': name,
            'email': email,
            'enrollment_no': enrollment,
            'branch': branch,
            'password': password,
            'is_admin': role_flag,
            'proxy_flag': False
        }

        # 2. SAVE TO ROOT COLLECTION (For Login)
        db.collection('users').document(email).set(user_data)

        # 3. SAVE TO BRANCH-SPECIFIC COLLECTION (For Attendance/Lists)
        if role_flag == 0 and branch:
            # This creates the document in IT2_users specifically
            db.collection(f"{branch}_users").document(email).set(user_data)
            print(f"DEBUG: Student added to {branch}_users silo.")

        flash("Registration Successful!")
        return redirect(url_for('index'))

    except Exception as e:
        print(f"Registration Error: {e}")
        return redirect(url_for('index'))


@app.route('/login', methods=['POST'])
def login():
    session.clear()
    email = request.form.get('email')
    password = hash_password(request.form.get('password'))
    
    user_doc = db.collection('users').document(email).get()
    
    if user_doc.exists:
        user_data = user_doc.to_dict()
        if user_data['password'] == password:
            session['user'] = user_data
            if str(user_data.get('is_admin')) == '1':
                return redirect(url_for('professor_dashboard'))
            return redirect(url_for('student_dashboard'))
    
    flash("Invalid Credentials!")
    return redirect(url_for('index'))



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
    branches_ref = db.collection('branches').stream()
    branches = [doc.id for doc in branches_ref]
    return render_template('admin_db.html', user=session['user'], branches=branches)


# NEW: Professor Start Attendance Route (Sets the token and location)
@app.route('/professor/start-attendance', methods=['POST'])
def start_attendance():
    branch_id = request.form.get('branch_id')
    lat = float(request.form.get('lat'))
    lng = float(request.form.get('lng'))
    session_token = secrets.token_hex(4) 
    
    # Ensure this writes to the 'branches' collection
    db.collection('branches').document(branch_id).update({
        'attendance_active': True,  # THIS MUST BE TRUE
        'current_token': session_token,
        'lat': lat,
        'lng': lng,
        'last_started': firestore.SERVER_TIMESTAMP
    })
    return {"status": "success", "token": session_token}


@app.route('/submit-attendance', methods=['POST'])
def submit_attendance():
    try:
        # 1. Capture data from the mobile form
        enrollment = request.form.get('enrollment', '').strip()
        password = hash_password(request.form.get('password', ''))
        student_lat = float(request.form.get('lat'))
        student_lng = float(request.form.get('lng'))
        branch_id = request.form.get('branch_id', '').strip().upper()  # e.g., 'IT2'
        provided_token = request.form.get('token', '').strip()

        # 2. TARGET THE SILO: Search for student in the branch-specific collection
        branch_silo = f"{branch_id}_users"
        student_query = db.collection(branch_silo).where('enrollment_no', '==', enrollment).get()

        if not student_query:
            print(f"DEBUG: Search failed in {branch_silo} for Enrollment: {enrollment}")
            return "Error: Student not found in this branch registry!", 404

        # 3. VALIDATE PASSWORD
        student_doc = student_query[0]
        student_data = student_doc.to_dict()
        if student_data['password'] != password:
            return "Error: Incorrect Password!", 401

        # 4. SESSION SECURITY: Check if branch is active and token matches QR
        branch_doc = db.collection('branches').document(branch_id).get()
        if not branch_doc.exists:
            return "Error: Branch registry not found!", 404

        branch_data = branch_doc.to_dict()
        if not branch_data.get('attendance_active'):
            return "Error: Attendance session is currently closed!", 403
        
        if branch_data.get('current_token') != provided_token:
            return "Error: Invalid or Expired QR Code!", 403

        # 5. GEOFENCING: Calculate distance (30m limit)
        prof_coords = (branch_data['lat'], branch_data['lng'])
        student_coords = (student_lat, student_lng)
        distance = geodesic(student_coords, prof_coords).meters

        if distance <= 30:
            today_str = datetime.now().strftime('%Y-%m-%d')
            student_email = student_data.get('email')

            # UPDATE BRANCH-SPECIFIC ATTENDANCE LOG
            # Ref: IT2_attendance -> Document: 2026-04-15
            att_ref = db.collection(f"{branch_id}_attendance").document(today_str)
            
            # Use ArrayUnion to prevent duplicates and keep existing entries
            att_ref.update({
                'present_list': firestore.ArrayUnion([student_email]),
                'last_updated': firestore.SERVER_TIMESTAMP
            })

            # Also store a flat log for the student's dashboard history
            db.collection('attendance').add({
                'email': student_email,
                'branch': branch_id,
                'timestamp': firestore.SERVER_TIMESTAMP,
                'date': today_str
            })

            return "Success! Attendance marked.", 200
        
        return f"Too far! You are {round(distance)}m away from the professor.", 403

    except Exception as e:
        print(f"CRITICAL ERROR in submit_attendance: {e}")
        return f"Server Error: {str(e)}", 500
    


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

@app.route('/api/users/<branch_id>', methods=['GET'])
def get_branch_users(branch_id):
    if 'user' not in session or session['user'].get('is_admin') != 1:
        return jsonify({"error": "Unauthorized"}), 403
    try:
        users_ref = db.collection(f"{branch_id}_users").stream()
        today_str = datetime.now().strftime('%Y-%m-%d')
        att_doc = db.collection(f"{branch_id}_attendance").document(today_str).get()
        
        present_emails = att_doc.to_dict().get('present_list', []) if att_doc.exists else []

        student_list = []
        for doc in users_ref:
            if doc.id in ["init", "placeholder"]: continue
            u = doc.to_dict()
            student_list.append({
                "name": u.get('name'),
                "enrollment_no": u.get('enrollment_no'),
                "email": u.get('email'),
                "proxy_flag": u.get('proxy_flag', False),
                "is_present": u.get('email') in present_emails
            })
        return jsonify(student_list), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    

@app.route('/api/attendance/<branch_id>', methods=['POST'])
def post_attendance(branch_id):
    data = request.json
    date_str = data.get('date') # e.g., '2026-04-15'
    present_list = data.get('present_list', [])
    absent_list = data.get('absent_list', [])

    if not date_str:
        return jsonify({"error": "Date is required"}), 400

    try:
        # Reference: IT1_attendance -> Document: 2026-04-15
        attendance_ref = db.collection(f"{branch_id}_attendance").document(date_str)
        
        attendance_ref.set({
            'present_list': present_list,
            'absent_list': absent_list,
            'last_updated': firestore.SERVER_TIMESTAMP
        })
        
        return jsonify({"message": f"Attendance for {date_str} posted."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    

# 3. API to toggle Proxy Flag
@app.route('/api/toggle-proxy', methods=['POST'])
def toggle_proxy():
    email = request.form.get('email')
    branch_id = request.form.get('branch_id') # Ensure JS sends this
    status = request.form.get('status') == 'true'
    
    # Update in BOTH places so login and branch lists stay in sync
    db.collection('users').document(email).update({'proxy_flag': status})
    db.collection(f"{branch_id}_users").document(email).update({'proxy_flag': status})
    return jsonify({"success": True})



@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/api/create-branch', methods=['POST'])
def create_branch():
    branch_name = request.json.get('branch_name').strip().upper()
    if not branch_name:
        return jsonify({"error": "Branch name is required"}), 400

    # 1. Add to master Registry
    db.collection('branches').document(branch_name).set({
        'created_at': firestore.SERVER_TIMESTAMP,
        'attendance_active': False
    })

    # 2. Initialize Attendance Silo (for today)
    today_str = datetime.now().strftime('%Y-%m-%d')
    db.collection(f"{branch_name}_attendance").document(today_str).set({
        'present_list': [],
        'last_updated': firestore.SERVER_TIMESTAMP
    })

    # 3. Initialize Users Silo
    db.collection(f"{branch_name}_users").document("init").set({"status": "active"})

    # 4. --- THE FIX: Initialize Assignments Silo ---
    db.collection(f"{branch_name}_assignments").document("init").set({
        "status": "active",
        "description": "Master list for assignments"
    })

    return jsonify({"message": f"Branch {branch_name} and all silos initialized!"}), 201

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)