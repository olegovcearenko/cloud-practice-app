from flask import Flask, request, jsonify, session, Response
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime
import psycopg2
import gridfs
import os

app = Flask(__name__)
app.secret_key = "cloud-practice-secret-key"


# =========================
# PostgreSQL connection
# =========================

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "cloud_practice_db"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres")
    )


# =========================
# MongoDB connection
# =========================

def get_mongo_connection():
    mongo_uri = os.getenv(
        "MONGO_URI",
        "mongodb://mongoadmin:mongopass@mongo-service:27017/?authSource=admin"
    )

    mongo_db = os.getenv("MONGO_DB", "cloud_practice_files")

    client = MongoClient(mongo_uri)
    return client[mongo_db]

# =========================
# Database initialization
# =========================

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(80) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        );
    """)

    cur.execute("SELECT COUNT(*) FROM users WHERE username = 'admin';")
    admin_exists = cur.fetchone()[0]

    if admin_exists == 0:
        cur.execute("""
            INSERT INTO users (username, password_hash)
            VALUES (%s, %s);
        """, ("admin", generate_password_hash("admin123")))

    conn.commit()
    cur.close()
    conn.close()


def is_logged_in():
    return session.get("user") is not None


# =========================
# Pages
# =========================

@app.route("/")
def index():
    if not is_logged_in():
        return LOGIN_PAGE
    return DASHBOARD_PAGE


# =========================
# Authentication
# =========================

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()

    username = data.get("username")
    password = data.get("password")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM users WHERE username = %s;", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if user and check_password_hash(user[0], password):
        session["user"] = username
        return jsonify({"message": "Login successful"})

    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})


# =========================
# PDF documents - MongoDB GridFS
# =========================

@app.route("/documents/upload", methods=["POST"])
def upload_document():
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    if "pdf_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["pdf_file"]

    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are allowed"}), 400

    db = get_mongo_connection()
    fs = gridfs.GridFS(db)

    file_id = fs.put(
        file,
        filename=file.filename,
        content_type="application/pdf",
        uploaded_by=session.get("user"),
        upload_date=datetime.utcnow()
    )

    return jsonify({
        "message": "PDF document uploaded successfully",
        "file_id": str(file_id),
        "filename": file.filename
    }), 201


@app.route("/documents", methods=["GET"])
def list_documents():
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    db = get_mongo_connection()
    files_collection = db["fs.files"]

    documents = []

    for doc in files_collection.find().sort("uploadDate", -1):
        documents.append({
            "id": str(doc["_id"]),
            "filename": doc.get("filename"),
            "content_type": doc.get("contentType"),
            "upload_date": str(doc.get("uploadDate")),
            "length": doc.get("length")
        })

    return jsonify(documents)


@app.route("/documents/download/<file_id>", methods=["GET"])
def download_document(file_id):
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    db = get_mongo_connection()
    fs = gridfs.GridFS(db)

    try:
        file = fs.get(ObjectId(file_id))

        return Response(
            file.read(),
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename={file.filename}"
            }
        )

    except Exception:
        return jsonify({"error": "File not found"}), 404


# =========================
# HTML pages
# =========================

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="ro">
<head>
    <meta charset="UTF-8">
    <title>Login - Cloud Documents</title>
    <style>
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #eef2f7;
            display: flex;
            height: 100vh;
            align-items: center;
            justify-content: center;
        }

        .login-box {
            background: white;
            width: 380px;
            padding: 30px;
            border-radius: 14px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.12);
        }

        h2 {
            margin-top: 0;
            text-align: center;
            color: #1f2937;
        }

        p {
            text-align: center;
            color: #6b7280;
            font-size: 14px;
        }

        input {
            width: 100%;
            padding: 12px;
            margin-top: 12px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            box-sizing: border-box;
        }

        button {
            width: 100%;
            margin-top: 18px;
            padding: 12px;
            border: none;
            border-radius: 8px;
            background: #2563eb;
            color: white;
            font-weight: bold;
            cursor: pointer;
        }

        button:hover {
            background: #1d4ed8;
        }

        .error {
            color: #dc2626;
            text-align: center;
            margin-top: 12px;
        }

        .hint {
            background: #f3f4f6;
            padding: 10px;
            border-radius: 8px;
            margin-top: 18px;
            font-size: 13px;
            color: #374151;
        }
    </style>
</head>
<body>
    <div class="login-box">
        <h2>Cloud Document Manager</h2>
        <p>Autentificare în sistem</p>

        <input id="username" placeholder="Username">
        <input id="password" type="password" placeholder="Password">

        <button onclick="login()">Login</button>

        <div id="error" class="error"></div>

        <div class="hint">
            Utilizator implicit:<br>
            <b>admin</b> / <b>admin123</b>
        </div>
    </div>

    <script>
        async function login() {
            const username = document.getElementById("username").value;
            const password = document.getElementById("password").value;

            const response = await fetch("/login", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({username, password})
            });

            if (response.ok) {
                location.reload();
            } else {
                document.getElementById("error").innerText = "Date de autentificare incorecte";
            }
        }
    </script>
</body>
</html>
"""


DASHBOARD_PAGE = """
<!DOCTYPE html>
<html lang="ro">
<head>
    <meta charset="UTF-8">
    <title>Dashboard - Cloud Documents</title>
    <style>
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f3f4f6;
            color: #111827;
        }

        header {
            background: #111827;
            color: white;
            padding: 18px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        header h1 {
            font-size: 22px;
            margin: 0;
        }

        header button {
            background: #ef4444;
            border: none;
            color: white;
            padding: 9px 14px;
            border-radius: 8px;
            cursor: pointer;
        }

        .container {
            max-width: 1050px;
            margin: 30px auto;
            padding: 0 20px;
        }

        .card {
            background: white;
            padding: 22px;
            border-radius: 14px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.08);
            margin-bottom: 24px;
        }

        h2 {
            margin-top: 0;
            color: #1f2937;
        }

        input {
            padding: 11px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-family: Arial, sans-serif;
        }

        .add-btn {
            margin-left: 10px;
            padding: 11px 18px;
            border: none;
            border-radius: 8px;
            background: #2563eb;
            color: white;
            font-weight: bold;
            cursor: pointer;
        }

        .add-btn:hover {
            background: #1d4ed8;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 18px;
        }

        th {
            background: #e5e7eb;
            text-align: left;
            padding: 12px;
        }

        td {
            border-bottom: 1px solid #e5e7eb;
            padding: 12px;
            vertical-align: top;
        }

        a {
            color: #2563eb;
            font-weight: bold;
            text-decoration: none;
        }

        .info {
            color: #4b5563;
            font-size: 14px;
            margin-bottom: 16px;
        }
    </style>
</head>
<body>
    <header>
        <h1>Cloud Document Manager - CI/CD Test</h1>
        <button onclick="logout()">Logout</button>
    </header>

    <div class="container">
        <div class="card">
            <h2>Încărcare document PDF</h2>
            <div class="info">
                PostgreSQL este utilizat pentru autentificarea utilizatorului.
                MongoDB GridFS este utilizat pentru stocarea documentelor PDF.
            </div>

            <input type="file" id="pdf_file" accept="application/pdf">
            <button class="add-btn" onclick="uploadPDF()">Încarcă PDF</button>
        </div>

        <div class="card">
            <h2>Lista documentelor PDF</h2>
            <table>
                <thead>
                    <tr>
                        <th>Nume fișier</th>
                        <th>Dimensiune</th>
                        <th>Data încărcării</th>
                        <th>Acțiune</th>
                    </tr>
                </thead>
                <tbody id="documents-table"></tbody>
            </table>
        </div>
    </div>

    <script>
        async function uploadPDF() {
            const fileInput = document.getElementById("pdf_file");
            const file = fileInput.files[0];

            if (!file) {
                alert("Selectează un fișier PDF");
                return;
            }

            const formData = new FormData();
            formData.append("pdf_file", file);

            const response = await fetch("/documents/upload", {
                method: "POST",
                body: formData
            });

            if (response.ok) {
                fileInput.value = "";
                loadDocuments();
            } else {
                alert("Eroare la încărcarea documentului PDF");
            }
        }

        async function loadDocuments() {
            const response = await fetch("/documents");
            const documents = await response.json();

            const table = document.getElementById("documents-table");
            table.innerHTML = "";

            documents.forEach(doc => {
                const row = document.createElement("tr");

                row.innerHTML = `
                    <td>${doc.filename}</td>
                    <td>${doc.length} bytes</td>
                    <td>${doc.upload_date}</td>
                    <td>
                        <a href="/documents/download/${doc.id}" target="_blank">Deschide PDF</a>
                    </td>
                `;

                table.appendChild(row);
            });
        }

        async function logout() {
            await fetch("/logout", {
                method: "POST"
            });

            location.reload();
        }

        loadDocuments();
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)