import os
import firebase_admin
from firebase_admin import credentials, firestore

# 1. Check if the key exists (Local Development)
if os.path.exists("serviceAccountKey.json"):
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
    print("Running locally with JSON key")
else:
    # 2. If no key, use default credentials (Live Hosting)
    # Firebase Hosting/Cloud Run automatically provides these
    firebase_admin.initialize_app()
    print("Running on Firebase/Cloud Run")

db = firestore.client()