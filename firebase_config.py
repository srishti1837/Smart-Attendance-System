from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

# Initialize Firebase
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()