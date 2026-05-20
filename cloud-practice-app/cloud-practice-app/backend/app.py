from datetime import datetime, timezone
from io import BytesIO
import os

import psycopg2
from bson import Binary, ObjectId
from flask import Flask, jsonify, request, session
from pypdf import PdfReader
from pymongo import MongoClient
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = "cloud-practice-secret-key"

ALLOWED_EXTENSIONS = {"pdf", "txt"}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024

_mongo_client = None


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "cloud_practice_db"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres")
    )


def get_mongo_collection():
    global _mongo_client

    if _mongo_client is None:
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
        _mongo_client = MongoClient(mongo_uri)

    mongo_db_name = os.getenv("MONGO_DB", "cloud_practice_files")
    mongo_collection_name = os.getenv("MONGO_COLLECTION", "work_documents")

    return _mongo_client[mongo_db_name][mongo_collection_name]


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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS practical_works (
            id SERIAL PRIMARY KEY,
            title VARCHAR(150) NOT NULL,
            topic VARCHAR(150) NOT NULL,
            description TEXT,
            status VARCHAR(50) DEFAULT 'neinceputa'
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS practical_work_documents (
            id SERIAL PRIMARY KEY,
            work_id INTEGER NOT NULL REFERENCES practical_works(id) ON DELETE CASCADE,
            original_filename VARCHAR(255) NOT NULL,
            file_extension VARCHAR(10) NOT NULL,
            content_type VARCHAR(120) NOT NULL,
            mongo_document_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_practical_work_documents_work_id
        ON practical_work_documents(work_id);
    """)

    cur.execute("SELECT COUNT(*) FROM users WHERE username = 'admin';")
    admin_exists = cur.fetchone()[0]

    if admin_exists == 0:
        cur.execute(
            """
            INSERT INTO users (username, password_hash)
            VALUES (%s, %s);
            """,
            ("admin", generate_password_hash("admin123"))
        )

    conn.commit()
    cur.close()
    conn.close()


def is_logged_in():
    return session.get("user") is not None


def extract_file_extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def parse_document_content(file_bytes, file_extension):
    if file_extension == "txt":
        return file_bytes.decode("utf-8", errors="replace")

    if file_extension == "pdf":
        reader = PdfReader(BytesIO(file_bytes))
        pages = []

        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages.append(page_text.strip())

        return "\n\n".join(pages)

    raise ValueError("Unsupported file type")


@app.route("/")
def index():
    if not is_logged_in():
        return LOGIN_PAGE
    return DASHBOARD_PAGE


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


@app.route("/works", methods=["GET"])
def get_works():
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            pw.id,
            pw.title,
            pw.topic,
            pw.description,
            pw.status,
            COALESCE(docs.docs_count, 0) AS docs_count
        FROM practical_works pw
        LEFT JOIN (
            SELECT work_id, COUNT(*) AS docs_count
            FROM practical_work_documents
            GROUP BY work_id
        ) docs ON docs.work_id = pw.id
        ORDER BY pw.id;
        """
    )

    rows = cur.fetchall()
    cur.close()
    conn.close()

    works = []
    for row in rows:
        works.append({
            "id": row[0],
            "title": row[1],
            "topic": row[2],
            "description": row[3],
            "status": row[4],
            "documents_count": row[5]
        })

    return jsonify(works)


@app.route("/works", methods=["POST"])
def add_work():
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()

    title = data.get("title")
    topic = data.get("topic")
    description = data.get("description", "")
    status = data.get("status", "neinceputa")

    if not title or not topic:
        return jsonify({"error": "Title and topic are required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO practical_works (title, topic, description, status)
        VALUES (%s, %s, %s, %s)
        RETURNING id;
        """,
        (title, topic, description, status)
    )

    new_id = cur.fetchone()[0]

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"message": "Practical work added", "id": new_id}), 201


@app.route("/works/<int:work_id>", methods=["DELETE"])
def delete_work(work_id):
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT mongo_document_id FROM practical_work_documents WHERE work_id = %s;",
        (work_id,)
    )
    mongo_ids = [row[0] for row in cur.fetchall()]

    if mongo_ids:
        mongo_collection = get_mongo_collection()
        object_ids = []
        for mongo_id in mongo_ids:
            try:
                object_ids.append(ObjectId(mongo_id))
            except Exception:
                continue

        if object_ids:
            mongo_collection.delete_many({"_id": {"$in": object_ids}})

    cur.execute("DELETE FROM practical_works WHERE id = %s;", (work_id,))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"message": "Practical work deleted"})


@app.route("/works/<int:work_id>/documents", methods=["GET"])
def list_work_documents(work_id):
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM practical_works WHERE id = %s;", (work_id,))
    work_exists = cur.fetchone()

    if not work_exists:
        cur.close()
        conn.close()
        return jsonify({"error": "Practical work not found"}), 404

    cur.execute(
        """
        SELECT id, original_filename, file_extension, created_at
        FROM practical_work_documents
        WHERE work_id = %s
        ORDER BY created_at DESC;
        """,
        (work_id,)
    )

    rows = cur.fetchall()
    cur.close()
    conn.close()

    documents = []
    for row in rows:
        documents.append({
            "id": row[0],
            "filename": row[1],
            "file_extension": row[2],
            "created_at": row[3].isoformat() if row[3] else None
        })

    return jsonify(documents)


@app.route("/works/<int:work_id>/documents", methods=["POST"])
def upload_work_document(work_id):
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    uploaded_file = request.files.get("file")
    if uploaded_file is None or not uploaded_file.filename:
        return jsonify({"error": "Missing file upload"}), 400

    file_extension = extract_file_extension(uploaded_file.filename)
    if file_extension not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Only PDF and TXT files are accepted"}), 400

    file_bytes = uploaded_file.read()
    if len(file_bytes) == 0:
        return jsonify({"error": "Empty file"}), 400

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        return jsonify({"error": "File too large. Max size is 10 MB"}), 400

    try:
        parsed_content = parse_document_content(file_bytes, file_extension)
    except Exception:
        return jsonify({"error": "Could not parse document content"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM practical_works WHERE id = %s;", (work_id,))
    work_exists = cur.fetchone()

    if not work_exists:
        cur.close()
        conn.close()
        return jsonify({"error": "Practical work not found"}), 404

    mongo_collection = get_mongo_collection()

    mongo_document = {
        "work_id": work_id,
        "filename": uploaded_file.filename,
        "file_extension": file_extension,
        "content_type": uploaded_file.mimetype or "application/octet-stream",
        "raw_content": Binary(file_bytes),
        "parsed_content": parsed_content,
        "uploaded_at": datetime.now(timezone.utc)
    }

    inserted_mongo_id = mongo_collection.insert_one(mongo_document).inserted_id

    try:
        cur.execute(
            """
            INSERT INTO practical_work_documents (
                work_id,
                original_filename,
                file_extension,
                content_type,
                mongo_document_id
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                work_id,
                uploaded_file.filename,
                file_extension,
                uploaded_file.mimetype or "application/octet-stream",
                str(inserted_mongo_id)
            )
        )
        doc_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        mongo_collection.delete_one({"_id": inserted_mongo_id})
        cur.close()
        conn.close()
        return jsonify({"error": "Could not save document metadata"}), 500

    cur.close()
    conn.close()

    return jsonify({"message": "Document uploaded", "id": doc_id}), 201


@app.route("/documents/<int:document_id>/content", methods=["GET"])
def get_document_content(document_id):
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, original_filename, file_extension, mongo_document_id, created_at
        FROM practical_work_documents
        WHERE id = %s;
        """,
        (document_id,)
    )
    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        return jsonify({"error": "Document not found"}), 404

    try:
        mongo_object_id = ObjectId(row[3])
    except Exception:
        return jsonify({"error": "Invalid Mongo reference"}), 500

    mongo_collection = get_mongo_collection()
    mongo_doc = mongo_collection.find_one({"_id": mongo_object_id})

    if not mongo_doc:
        return jsonify({"error": "Document content missing"}), 404

    return jsonify({
        "id": row[0],
        "filename": row[1],
        "file_extension": row[2],
        "created_at": row[4].isoformat() if row[4] else None,
        "content": mongo_doc.get("parsed_content", "")
    })


LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="ro">
<head>
    <meta charset="UTF-8">
    <title>Login - Cloud Practical Works</title>
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
            width: 360px;
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
        <h2>Cloud Practical Works</h2>
        <p>Autentificare in sistem</p>

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
    <title>Dashboard - Cloud Practical Works</title>
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
            width: auto;
            margin: 0;
        }

        .container {
            max-width: 1200px;
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

        .form-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
        }

        input, textarea, select {
            padding: 11px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-family: Arial, sans-serif;
            box-sizing: border-box;
        }

        textarea {
            grid-column: span 2;
            min-height: 80px;
            resize: vertical;
        }

        .add-btn {
            margin-top: 14px;
            padding: 12px 18px;
            border: none;
            border-radius: 8px;
            background: #2563eb;
            color: white;
            font-weight: bold;
            cursor: pointer;
            width: auto;
        }

        .add-btn:hover {
            background: #1d4ed8;
        }

        table {
            width: 100%;
            border-collapse: collapse;
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

        .delete-btn,
        .docs-btn,
        .upload-btn,
        .view-btn {
            border: none;
            padding: 7px 10px;
            border-radius: 6px;
            cursor: pointer;
            color: white;
            width: auto;
            margin: 0;
        }

        .delete-btn {
            background: #dc2626;
        }

        .docs-btn {
            background: #2563eb;
            margin-right: 6px;
        }

        .upload-btn {
            background: #059669;
            margin-left: 6px;
        }

        .view-btn {
            background: #4f46e5;
        }

        .status {
            font-weight: bold;
            color: #2563eb;
        }

        .documents-panel {
            display: none;
        }

        .documents-panel.active {
            display: block;
        }

        .docs-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            flex-wrap: wrap;
        }

        .docs-upload {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
            margin: 12px 0;
        }

        #docs-list {
            margin-top: 12px;
        }

        .doc-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            padding: 10px;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            margin-bottom: 8px;
        }

        .doc-name {
            font-weight: 600;
            color: #1f2937;
        }

        .doc-meta {
            color: #6b7280;
            font-size: 13px;
            margin-top: 4px;
        }

        .empty {
            color: #6b7280;
        }

        .preview-box {
            margin-top: 16px;
        }

        .preview-box pre {
            background: #0f172a;
            color: #f8fafc;
            padding: 14px;
            border-radius: 8px;
            white-space: pre-wrap;
            max-height: 360px;
            overflow: auto;
        }

        .hint-small {
            color: #6b7280;
            font-size: 13px;
            margin-top: 6px;
        }

        @media (max-width: 900px) {
            .form-grid {
                grid-template-columns: 1fr;
            }

            textarea {
                grid-column: auto;
            }

            th:nth-child(4),
            td:nth-child(4) {
                display: none;
            }
        }
    </style>
</head>
<body>
    <header>
        <h1>Cloud Practical Works Manager</h1>
        <button onclick="logout()">Logout</button>
    </header>

    <div class="container">
        <div class="card">
            <h2>Adauga lucrare practica</h2>

            <div class="form-grid">
                <input id="title" placeholder="Titlul lucrarii">
                <input id="topic" placeholder="Tema">
                <textarea id="description" placeholder="Descriere"></textarea>
                <select id="status">
                    <option value="neinceputa">Neinceputa</option>
                    <option value="in lucru">In lucru</option>
                    <option value="finalizata">Finalizata</option>
                    <option value="verificata">Verificata</option>
                </select>
            </div>

            <button class="add-btn" onclick="addWork()">Adauga lucrare</button>
        </div>

        <div class="card">
            <h2>Lista lucrarilor practice</h2>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Titlu</th>
                        <th>Tema</th>
                        <th>Descriere</th>
                        <th>Status</th>
                        <th>Documente</th>
                        <th>Actiune</th>
                    </tr>
                </thead>
                <tbody id="works-table"></tbody>
            </table>
        </div>

        <div id="documents-panel" class="card documents-panel">
            <div class="docs-header">
                <h2 id="docs-work-title">Documente lucrare</h2>
            </div>

            <div class="docs-upload">
                <input id="document-file" type="file" accept=".pdf,.txt">
                <button class="upload-btn" onclick="uploadDocument()">Incarca document</button>
            </div>
            <div class="hint-small">Sunt acceptate doar fisiere PDF si TXT, max 10 MB.</div>

            <div id="docs-list"></div>

            <div class="preview-box">
                <h3>Previzualizare continut</h3>
                <pre id="doc-preview">Selecteaza un document pentru vizualizare.</pre>
            </div>
        </div>
    </div>

    <script>
        let selectedWorkId = null;
        const workTitles = {};

        function escapeHtml(value) {
            return (value || "")
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#039;");
        }

        async function loadWorks() {
            const response = await fetch("/works");
            if (!response.ok) {
                location.reload();
                return;
            }

            const works = await response.json();

            const table = document.getElementById("works-table");
            table.innerHTML = "";

            works.forEach(work => {
                workTitles[work.id] = work.title || "";
                const row = document.createElement("tr");

                row.innerHTML = `
                    <td>${work.id}</td>
                    <td>${escapeHtml(work.title)}</td>
                    <td>${escapeHtml(work.topic)}</td>
                    <td>${escapeHtml(work.description || "")}</td>
                    <td class="status">${escapeHtml(work.status)}</td>
                    <td>${work.documents_count}</td>
                    <td>
                        <button class="docs-btn" onclick="openDocuments(${work.id})">Documente</button>
                        <button class="delete-btn" onclick="deleteWork(${work.id})">Sterge</button>
                    </td>
                `;

                table.appendChild(row);
            });
        }

        async function addWork() {
            const title = document.getElementById("title").value;
            const topic = document.getElementById("topic").value;
            const description = document.getElementById("description").value;
            const status = document.getElementById("status").value;

            const response = await fetch("/works", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({title, topic, description, status})
            });

            if (response.ok) {
                document.getElementById("title").value = "";
                document.getElementById("topic").value = "";
                document.getElementById("description").value = "";
                loadWorks();
            } else {
                alert("Eroare la adaugarea lucrarii");
            }
        }

        async function deleteWork(id) {
            await fetch(`/works/${id}`, {
                method: "DELETE"
            });

            if (selectedWorkId === id) {
                selectedWorkId = null;
                document.getElementById("documents-panel").classList.remove("active");
            }

            loadWorks();
        }

        function openDocuments(workId) {
            selectedWorkId = workId;
            const panel = document.getElementById("documents-panel");
            panel.classList.add("active");
            const workTitle = workTitles[workId] || `Lucrare #${workId}`;
            document.getElementById("docs-work-title").innerText = `Documente pentru: ${workTitle}`;
            document.getElementById("doc-preview").innerText = "Selecteaza un document pentru vizualizare.";
            loadDocuments();
        }

        async function loadDocuments() {
            if (!selectedWorkId) {
                return;
            }

            const response = await fetch(`/works/${selectedWorkId}/documents`);
            const docsList = document.getElementById("docs-list");

            if (!response.ok) {
                docsList.innerHTML = '<p class="empty">Nu s-au putut incarca documentele.</p>';
                return;
            }

            const documents = await response.json();

            if (!documents.length) {
                docsList.innerHTML = '<p class="empty">Nu exista documente incarcate.</p>';
                return;
            }

            docsList.innerHTML = "";

            documents.forEach(documentItem => {
                const wrapper = document.createElement("div");
                wrapper.className = "doc-item";

                const info = document.createElement("div");
                info.innerHTML = `
                    <div class="doc-name">${escapeHtml(documentItem.filename)}</div>
                    <div class="doc-meta">Tip: ${escapeHtml(documentItem.file_extension.toUpperCase())}</div>
                `;

                const viewBtn = document.createElement("button");
                viewBtn.className = "view-btn";
                viewBtn.innerText = "Vizualizeaza";
                viewBtn.onclick = () => viewDocumentContent(documentItem.id);

                wrapper.appendChild(info);
                wrapper.appendChild(viewBtn);
                docsList.appendChild(wrapper);
            });
        }

        async function uploadDocument() {
            if (!selectedWorkId) {
                alert("Selecteaza mai intai o lucrare.");
                return;
            }

            const fileInput = document.getElementById("document-file");
            const selectedFile = fileInput.files[0];

            if (!selectedFile) {
                alert("Selecteaza un fisier PDF sau TXT.");
                return;
            }

            const formData = new FormData();
            formData.append("file", selectedFile);

            const response = await fetch(`/works/${selectedWorkId}/documents`, {
                method: "POST",
                body: formData
            });

            if (response.ok) {
                fileInput.value = "";
                await loadDocuments();
                await loadWorks();
            } else {
                const payload = await response.json();
                alert(payload.error || "Eroare la incarcare document");
            }
        }

        async function viewDocumentContent(documentId) {
            const response = await fetch(`/documents/${documentId}/content`);
            const preview = document.getElementById("doc-preview");

            if (!response.ok) {
                preview.innerText = "Continut indisponibil.";
                return;
            }

            const payload = await response.json();
            preview.innerText = payload.content || "Document fara continut text extras.";
        }

        async function logout() {
            await fetch("/logout", {
                method: "POST"
            });

            location.reload();
        }

        loadWorks();
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
