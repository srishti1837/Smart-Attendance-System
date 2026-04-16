from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from firebase_config import db
from firebase_admin import firestore
import hashlib
import qrcode
import io
import base64
import secrets # Used for dynamic tokens
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev_key_only')

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
    code = (request.form.get('reg_code') or '').strip()
    
    admin_secret = os.getenv('ADMIN_REG_CODE', '')
    student_secret = os.getenv('STUDENT_REG_CODE', '')

    if not code:
        flash("Please enter a registration code.", "error")
        return redirect(url_for('index')) # Point to your main page function

    if code == admin_secret and admin_secret != '':
        # We pass the code to the template so the Prof form can 're-verify' it on submission
        return render_template('register_prof.html', pass_code=code)
    
    elif code == student_secret and student_secret != '':
        try:
            branches_ref = db.collection('branches').stream()
            branches_list = [doc.id for doc in branches_ref]
            # Students don't need the secret passed because they select a branch
            return render_template('register.html', branches=branches_list)
        except Exception as e:
            return render_template('register.html', branches=[])
    
    else:
        flash("Invalid Registration Code! Please contact your Professor.", "error")
        return redirect(url_for('index'))
    

@app.route('/api/register', methods=['POST'])
def handle_registration():
    try:
        # 1. Capture Form Data
        email = request.form.get('email', '').strip().lower()
        name = request.form.get('name')
        enrollment = request.form.get('enrollment')
        branch = request.form.get('branch', '').strip().upper()
        password = hash_password(request.form.get('password'))
        
        # 2. Security Check (Admin Key)
        submitted_code = request.form.get('prof_admin_code', '').strip()
        admin_secret = os.getenv('ADMIN_REG_CODE', '')
        student_secret = os.getenv('STUDENT_REG_CODE', '')

        # Determine Role
        is_prof = (submitted_code == admin_secret and admin_secret != '')
        is_student = (submitted_code == student_secret and student_secret != '')

        # SECURITY GATE: If code matches neither secret, STOP immediately
        if not is_prof and not is_student:
            flash("Invalid Registration Code! No account created.", "error")
            return redirect(url_for('index')) 

        # 3. Create User Data
        role_flag = 1 if is_prof else 0
        user_data = {
            'name': name,
            'email': email,
            'enrollment_no': enrollment,
            'branch': branch if role_flag == 0 else "ADMIN",
            'password': password,
            'is_admin': role_flag,
            'proxy_flag': False
        }

        # 4. SAVE TO ROOT COLLECTION
        db.collection('users').document(email).set(user_data)

        # 5. SAVE TO BRANCH SILO (Only for Students)
        if role_flag == 0 and branch:
            db.collection(f"{branch}_users").document(email).set(user_data)

        flash("Registration Successful! Please Login.", "success")
        return redirect(url_for('index')) # Redirect to login

    except Exception as e:
        print(f"Registration Error: {e}")
        flash("Registration failed. Server error occurred.", "error")
        return redirect(url_for('index')) 
    


@app.route('/login', methods=['POST'])
def login():
    session.clear()
    email = request.form.get('email', '').strip().lower()
    password = hash_password(request.form.get('password'))
    
    user_doc = db.collection('users').document(email).get()
    
    if user_doc.exists:
        user_data = user_doc.to_dict()
        if user_data.get('password') == password:
            session['user_email'] = email
            session['branch_id'] = user_data.get('branch')
            
            # Use .get() and cast to int/str to avoid errors if the field is missing
            is_admin = user_data.get('is_admin', 0)
            
            if str(is_admin) == '1' or is_admin is True:
                return redirect(url_for('professor_dashboard'))
            return redirect(url_for('student_dashboard'))
    
    # If we reach here, login failed
    flash("Invalid Email or Password!", "error")
    return redirect(url_for('index')) # Matches your main page function



@app.route('/student-dashboard')
def student_dashboard():
    student_email = session.get('user_email')
    branch_id = session.get('branch_id') 

    if not student_email or not branch_id:
        return redirect(url_for('index'))

    # 1. Fetch Student Profile
    student_ref = db.collection("users").document(student_email).get()
    if not student_ref.exists:
        return f"Error: No record found for {student_email}", 404
        
    student_data = student_ref.to_dict()

    # --- NEW: Calculate Total Marks ---
    # We use float() because you mentioned storing them as floats
    mid = float(student_data.get('marks_mid', 0))
    end = float(student_data.get('marks_end', 0))
    cap = float(student_data.get('marks_cap', 0))
    total_marks = mid + end + cap
    # ----------------------------------

    # 2. Calculate Attendance
    sessions = db.collection(f"{branch_id}_attendance").stream()
    total_classes = 0
    attended_count = 0
    full_history = []

    for sess in sessions:
        if sess.id == 'init': continue
        total_classes += 1
        data = sess.to_dict()
        is_present = student_email in data.get('present_list', [])
        if is_present: attended_count += 1
        full_history.append({
            "date": sess.id.split('_')[0],
            "status": "Present" if is_present else "Absent"
        })

    # 3. Fetch Assignments
    assn_ref = db.collection(f"{branch_id}_assignments").stream()
    assignments_list = []
    for doc in assn_ref:
        if doc.id == 'init': continue
        a = doc.to_dict()
        # Clean email for dictionary key lookup
        email_key = student_email.replace('.', '_')
        assignments_list.append({
            "title": a.get('title'),
            "description": a.get('description'),
            "deadline": a.get('deadline'),
            "submitted": student_email in a.get('submissions', []),
            "submission_date": a.get('submission_dates', {}).get(email_key, 'N/A')
        })

    attendance_pct = round((attended_count / total_classes * 100), 1) if total_classes > 0 else 0

    return render_template('student_db.html', 
                           student=student_data,
                           branch_id=branch_id,
                           total_classes=total_classes,
                           attendance_records=attended_count,
                           attendance_percentage=attendance_pct,
                           full_history=full_history,
                           assignments=assignments_list,
                           total_marks=total_marks,  # Pass the new total here
                           footer_date="2026-04-15")


@app.route('/professor-dashboard')
def professor_dashboard():
    # Fetch from 'branches' collection
    branches_ref = db.collection('branches').stream()
    branches_list = [{'id': doc.id} for doc in branches_ref] # List of dicts
    
    return render_template('admin_db.html', 
                           user=session.get('user'), 
                           branches=branches_list)



@app.route('/professor/start-attendance', methods=['POST'])
def start_attendance():
    try:
        branch_id = request.form.get('branch_id', '').strip().upper()
        # Removed lat/lng capturing

        branch_ref = db.collection('branches').document(branch_id)
        branch_data = branch_ref.get().to_dict()

        if branch_data.get('attendance_active'):
            existing_id = branch_data.get('current_session_id')
            doc_check = db.collection(f"{branch_id}_attendance").document(existing_id).get()
            
            if doc_check.exists:
                return jsonify({
                    "status": "success", 
                    "token": branch_data.get('current_token'), 
                    "session_id": existing_id
                })

        session_id = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        session_token = secrets.token_hex(4)

        # 1. Create document (Removed lat/lng)
        db.collection(f"{branch_id}_attendance").document(session_id).set({
            'present_list': [],
            'start_time': firestore.SERVER_TIMESTAMP,
            'status': 'active',
            'token': session_token
        })

        # 2. Update Registry (Removed lat/lng)
        branch_ref.update({
            'attendance_active': True,
            'current_session_id': session_id,
            'current_token': session_token
        })

        return jsonify({"status": "success", "token": session_token, "session_id": session_id})

    except Exception as e:
        print(f"ERROR: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/professor/stop-attendance/<branch_id>')
def stop_attendance(branch_id):
    try:
        branch_id = branch_id.upper()
        # 1. Look up which session is currently active
        branch_ref = db.collection('branches').document(branch_id)
        branch_data = branch_ref.get().to_dict()
        
        active_session_id = branch_data.get('current_session_id')

        # 2. Update the session document to "closed"
        if active_session_id and active_session_id != 'none':
            db.collection(f"{branch_id}_attendance").document(active_session_id).update({
                'status': 'closed',
                'end_time': firestore.SERVER_TIMESTAMP
            })

        # 3. Disable the session in the master registry
        branch_ref.update({
            'attendance_active': False,
            'current_token': 'none'
        })

        return jsonify({"status": "success", "message": "Session closed"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    

@app.route('/submit-attendance', methods=['POST'])
def submit_attendance():
    try:
        # 1. Capture data (Removed student_lat/lng)
        enrollment = request.form.get('enrollment', '').strip()
        password = hash_password(request.form.get('password', ''))
        branch_id = request.form.get('branch_id', '').strip().upper() 
        provided_token = request.form.get('token', '').strip()

        # 2. SESSION VALIDATION
        branch_doc_ref = db.collection('branches').document(branch_id).get()
        if not branch_doc_ref.exists:
            return "Error: Branch registry not found!", 404

        branch_data = branch_doc_ref.to_dict()
        if not branch_data.get('attendance_active'):
            return "Error: No active attendance session found!", 403
        
        if branch_data.get('current_token') != provided_token:
            return "Error: Invalid or Expired QR Code!", 403

        current_session_id = branch_data.get('current_session_id')

        # 3. STUDENT IDENTITY
        branch_silo = f"{branch_id}_users" 
        student_query = db.collection(branch_silo).where('enrollment_no', '==', enrollment).get()

        if not student_query:
            return "Error: Student not found in this branch!", 404

        student_doc = student_query[0]
        student_data = student_doc.to_dict()
        
        if student_data['password'] != password:
            return "Error: Incorrect Password!", 401

        student_email = student_data.get('email')

        # 4. DUPLICATE CHECK
        session_ref = db.collection(f"{branch_id}_attendance").document(current_session_id)
        session_data = session_ref.get().to_dict()
        
        if student_email in session_data.get('present_list', []):
            return "Already Marked! You are already on the attendance list.", 200
        
        # 6. LOG ATTENDANCE
        session_ref.update({
            'present_list': firestore.ArrayUnion([student_email]),
            'last_updated': firestore.SERVER_TIMESTAMP
        })

        return "Success! Attendance marked for this session.", 200

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
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
    qr_data = f"https://smart-attendance-system-elst.onrender.com/mark-attendance-page/{branch_id}?token={token}"
    
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
    # Check if 'user' exists in session
    user_data = session.get('user')
    
    if not user_data:
        flash("Session expired. Please login.", "error")
        return redirect(url_for('index'))

    # Robust check for is_admin (handles int, string, or boolean)
    is_admin = user_data.get('is_admin')
    if str(is_admin) != '1' and is_admin is not True:
        flash("Access Denied: Professor account required.", "error")
        return redirect(url_for('index'))

    # Fetch the session details to display (QR Token, etc.)
    branch_ref = db.collection('branches').document(branch_id).get()
    if not branch_ref.exists:
        flash("Branch not found.", "error")
        return redirect(url_for('professor_dashboard'))

    branch_data = branch_ref.to_dict()
    
    return render_template('live_session.html', 
                           branch_id=branch_id,
                           token=branch_data.get('current_token'),
                           session_id=branch_data.get('current_session_id'))


@app.route('/api/users/<branch_id>', methods=['GET'])
def get_branch_users(branch_id):
    try:
        # 1. Get the current active session ID from the branch registry
        branch_doc = db.collection('branches').document(branch_id).get().to_dict()
        active_session = branch_doc.get('current_session_id')
        
        # 2. Get the list of people who marked attendance in THIS session
        present_emails = []
        if active_session:
            att_doc = db.collection(f"{branch_id}_attendance").document(active_session).get()
            if att_doc.exists:
                present_emails = att_doc.to_dict().get('present_list', [])

        # 3. Get all students registered in this branch silo
        users_ref = db.collection(f"{branch_id}_users").stream()
        
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
    branch_name = request.json.get('branch_name', '').strip().upper()
    if not branch_name:
        return jsonify({"error": "Branch name is required"}), 400

    # 1. Add to Master Registry
    # We initialize current_session_id as 'none' so the logic doesn't crash later
    db.collection('branches').document(branch_name).set({
        'created_at': firestore.SERVER_TIMESTAMP,
        'attendance_active': False,
        'current_session_id': 'none',
        'current_token': 'none'
    })

    # 2. Initialize the Collections (Creating 'init' docs to wake them up)
    db.collection(f"{branch_name}_attendance").document("init").set({"status": "ready"})
    db.collection(f"{branch_name}_users").document("init").set({"status": "active"})
    db.collection(f"{branch_name}_assignments").document("init").set({"status": "active"})

    return jsonify({"message": f"Branch {branch_name} infrastructure ready!"}), 201

@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    student_email = session.get('user_email')
    branch_id = session.get('branch_id')
    feedback_text = request.form.get('feedback').strip()

    # 1. Word Count Validation (Python side)
    words = feedback_text.split()
    if len(words) > 50:
        return "Error: Feedback exceeds 50 words.", 400

    # 2. Check if student already commented
    # We store all comments in a single collection 'global_comments'
    comment_ref = db.collection('global_comments').document(student_email).get()
    
    if comment_ref.exists:
        return "Error: You have already submitted feedback.", 403

    # 3. Fetch student details for the professor's view
    student_data = db.collection("users").document(student_email).get().to_dict()

    # 4. Store in 'global_comments'
    db.collection('global_comments').document(student_email).set({
        'name': student_data.get('name'),
        'enrollment_no': student_data.get('enrollment_no'),
        'branch': branch_id,
        'comment': feedback_text,
        'timestamp': firestore.SERVER_TIMESTAMP
    })

    return "Feedback shared successfully!", 200

@app.route('/professor/view-comments')
def view_comments():
    comments_ref = db.collection('global_comments').order_by('timestamp', direction=firestore.Query.DESCENDING).stream()
    comments_list = [doc.to_dict() for doc in comments_ref]
    return render_template('view_comments.html', comments=comments_list)

# 1. Render the Marking Table Page
@app.route('/professor/mark-assignments/<branch_id>')
def mark_assignments_page(branch_id):
    # Fetch all students in this branch from the 'users' collection
    students = db.collection('users').where('branch', '==', branch_id).get()
    student_list = [s.to_dict() for s in students]

    # Fetch all assignments for this branch
    assns = db.collection(f"{branch_id}_assignments").stream()
    assignments_list = []
    for a in assns:
        if a.id == 'init': continue
        data = a.to_dict()
        data['id'] = a.id
        assignments_list.append(data)

    return render_template('mark_assignments.html', 
                           branch_id=branch_id, 
                           students=student_list, 
                           assignments=assignments_list)

# 2. API to Add a New Assignment (From the Modal)
@app.route('/api/create-assignment', methods=['POST'])
def create_assignment_global():
    data = request.json
    assn_id = f"assn{data.get('num')}"
    
    # Fetch all branches from the database
    branches = db.collection('branches').stream()
    
    for branch in branches:
        branch_name = branch.id
        # Create identical assignment for each branch silo
        db.collection(f"{branch_name}_assignments").document(assn_id).set({
            'title': data.get('title'),
            'description': data.get('desc'),
            'deadline': data.get('deadline'),
            'submissions': [],
            'submission_dates': {},
            'timestamp': firestore.SERVER_TIMESTAMP
        })
        
    return jsonify({"status": "success", "message": "Assignment published to all branches"})



# 3. API to Check/Uncheck an assignment for a student
@app.route('/api/update-assignment', methods=['POST'])
def update_assignment():
    data = request.json
    branch_id = data.get('branch_id')
    email = data.get('email')
    assn_id = data.get('assignment_id')
    is_checked = data.get('status')
    
    # Clean email for dictionary keys (Firestore doesn't like dots in keys)
    email_key = email.replace('.', '_')
    today_date = datetime.now().strftime('%Y-%m-%d') if is_checked else ""
    
    doc_ref = db.collection(f"{branch_id}_assignments").document(assn_id)
    
    if is_checked:
        doc_ref.update({
            'submissions': firestore.ArrayUnion([email]),
            f'submission_dates.{email_key}': today_date
        })
    else:
        doc_ref.update({
            'submissions': firestore.ArrayRemove([email]),
            f'submission_dates.{email_key}': firestore.DELETE_FIELD
        })
        
    return jsonify({"status": "success", "date": today_date})

@app.route('/professor/add-marks/<branch_id>')
def add_marks_page(branch_id):
    # Fetch students from the main users collection for this branch
    students = db.collection('users').where('branch', '==', branch_id).get()
    student_list = [s.to_dict() for s in students]
    
    # Fetch existing marks from branch-specific collection
    marks_ref = db.collection(f"{branch_id}_marks").stream()
    marks_data = {doc.id: doc.to_dict() for doc in marks_ref}
    
    return render_template('add_marks.html', branch_id=branch_id, students=student_list, marks=marks_data)


@app.route('/api/save-all-marks', methods=['POST'])
def save_all_marks():
    try:
        data = request.json
        branch_id = data.get('branch_id')
        marks_list = data.get('marks_data', [])

        # Using a Firestore Batch for efficiency (saves all in one network call)
        batch = db.batch()

        for entry in marks_list:
            email = entry['email']
            mid = float(entry['mid'])
            end = float(entry['end'])
            cap = float(entry['cap'])
            
            # 1. Update branch-specific record
            branch_mark_ref = db.collection(f"{branch_id}_marks").document(email)
            batch.set(branch_mark_ref, {
                'mid_term': mid,
                'end_term': end,
                'cap_marks': cap,
                'last_updated': firestore.SERVER_TIMESTAMP
            })

            # 2. Update main user document for dashboard display
            user_ref = db.collection("users").document(email)
            batch.update(user_ref, {
                'marks_mid': mid,
                'marks_end': end,
                'marks_cap': cap
            })

        batch.commit() # Execute all writes together
        return jsonify({"status": "success"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
        

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
