import math
import hashlib
import hmac
import base64
import io
import json
import struct
import time
import secrets
import socket
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
import qrcode

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "phitest.db"
STATIC_INDEX = BASE_DIR / "static" / "index.html"
MIN_UNIQUE_CHARTS = 3
SESSION_COOKIE = "phitest_session"


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def database():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
    with database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS charts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            song_id INTEGER NOT NULL,
            difficulty TEXT NOT NULL,
            level REAL,
            constant REAL,
            FOREIGN KEY(song_id) REFERENCES songs(id),
            UNIQUE(song_id, difficulty)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'train',
            power REAL NOT NULL DEFAULT 1.0,
            user_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS session_charts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            chart_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(chart_id) REFERENCES charts(id)
        );
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_chart_id INTEGER NOT NULL UNIQUE,
            score INTEGER NOT NULL,
            played_at TEXT NOT NULL,
            accuracy REAL,
            FOREIGN KEY(session_chart_id) REFERENCES session_charts(id)
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            totp_secret TEXT,
            authenticator_setup_recovered INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)
        session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        if "kind" not in session_columns:
            db.execute("ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'train'")
        if "power" not in session_columns:
            db.execute("ALTER TABLE sessions ADD COLUMN power REAL NOT NULL DEFAULT 1.0")
        if "user_id" not in session_columns:
            db.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
        record_columns = {row[1] for row in db.execute("PRAGMA table_info(records)")}
        if "accuracy" not in record_columns:
            db.execute("ALTER TABLE records ADD COLUMN accuracy REAL")
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "authenticator_setup_recovered" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN authenticator_setup_recovered INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE users SET authenticator_setup_recovered=1")
        db.execute("UPDATE sessions SET user_id=(SELECT MIN(id) FROM users) WHERE user_id IS NULL")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
 

app = FastAPI(title="PhiTest Training", lifespan=lifespan)


def password_digest(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def verify_totp(secret: str, code: str) -> bool:
    if not code.isdigit() or len(code) != 6:
        return False
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(time.time()) // 30
    for offset in (-1, 0, 1):
        digest = hmac.new(key, struct.pack(">Q", counter + offset), hashlib.sha1).digest()
        position = digest[-1] & 0x0F
        value = (struct.unpack(">I", digest[position:position + 4])[0] & 0x7FFFFFFF) % 1_000_000
        if secrets.compare_digest(f"{value:06d}", code):
            return True
    return False


def authenticate(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Please log in first")
    with database() as db:
        row = db.execute("""
            SELECT users.id, users.username FROM auth_sessions
            JOIN users ON users.id=auth_sessions.user_id
            WHERE auth_sessions.token_hash=? AND auth_sessions.expires_at>?
        """, (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
    if not row:
        raise HTTPException(401, "Your session has expired. Please log in again")
    return dict(row)


class CredentialsInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    code: str = Field(pattern=r"^\d{6}$")

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Username cannot be empty")
        return value


class RegistrationInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class PasswordResetInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=8, max_length=256)
    code: str = Field(pattern=r"^\d{6}$")


class AuthenticatorSetupInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)


def authenticator_setup(username: str, secret: str) -> dict:
    uri = f"otpauth://totp/PhiTest:{username}?secret={secret}&issuer=PhiTest"
    image = qrcode.make(uri)
    output = io.BytesIO()
    image.save(output, "PNG")
    return {"username": username, "totp_secret": secret, "otpauth_url": uri,
            "qr_code": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")}


@app.post("/api/auth/register")
def register(data: RegistrationInput, request: Request):
    if request.headers.get("origin") and request.headers["origin"] != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Invalid request origin")
    secret = new_totp_secret()
    salt = secrets.token_hex(16)
    username = data.username.strip()
    if not username:
        raise HTTPException(400, "Username cannot be empty")
    try:
        with database() as db:
            db.execute("""
                                INSERT INTO users(username,password_salt,password_hash,totp_secret,authenticator_setup_recovered,created_at)
                                VALUES(?,?,?,?,?,?)
            """, (username, salt, password_digest(data.password, salt), secret,
                                    0, datetime.now().isoformat()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username already exists")
    with database() as db:
        db.execute("UPDATE sessions SET user_id=(SELECT id FROM users WHERE username=? COLLATE NOCASE) WHERE user_id IS NULL",
                   (username,))
    return authenticator_setup(username, secret)


@app.post("/api/auth/authenticator-setup")
def existing_authenticator_setup(data: AuthenticatorSetupInput):
    with database() as db:
        row = db.execute("SELECT id,username,password_salt,password_hash,totp_secret,authenticator_setup_recovered FROM users WHERE username=? COLLATE NOCASE",
                         (data.username.strip(),)).fetchone()
    if (not row or not secrets.compare_digest(password_digest(data.password, row["password_salt"]),
                                               row["password_hash"])):
        raise HTTPException(401, "Incorrect username or password")
    if row["authenticator_setup_recovered"]:
        raise HTTPException(409, "Authenticator setup recovery has already been used")
    with database() as db:
        updated = db.execute("""
            UPDATE users SET authenticator_setup_recovered=1
            WHERE id=? AND authenticator_setup_recovered=0
        """, (row["id"],)).rowcount
    if updated != 1:
        raise HTTPException(409, "Authenticator setup recovery has already been used")
    return authenticator_setup(row["username"], row["totp_secret"])


@app.post("/api/auth/login")
def login(data: CredentialsInput, response: Response):
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                         (data.username,)).fetchone()
    if (not row or not secrets.compare_digest(password_digest(data.password, row["password_salt"]), row["password_hash"])
            or not row["totp_secret"] or not verify_totp(row["totp_secret"], data.code)):
        raise HTTPException(401, "Incorrect username, password, or authenticator code")
    token = secrets.token_urlsafe(32)
    with database() as db:
        db.execute("INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), row["id"], int(time.time()) + 2592000))
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=2592000)
    return {"username": row["username"]}


@app.post("/api/auth/reset-password")
def reset_password(data: PasswordResetInput):
    with database() as db:
        row = db.execute("SELECT id,totp_secret FROM users WHERE username=? COLLATE NOCASE",
                         (data.username,)).fetchone()
        if not row or not verify_totp(row["totp_secret"] or "", data.code):
            raise HTTPException(401, "Incorrect username or authenticator code")
        salt = secrets.token_hex(16)
        db.execute("UPDATE users SET password_salt=?,password_hash=? WHERE id=?",
                   (salt, password_digest(data.new_password, salt), row["id"]))
        db.execute("DELETE FROM auth_sessions WHERE user_id=?", (row["id"],))
    return {"message": "Password reset. Please log in again"}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with database() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?",
                       (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(SESSION_COOKIE)
    return {"message": "Logged out"}


@app.get("/api/auth/me")
def current_account(user=Depends(authenticate)):
    return {"username": user["username"]}


@app.get("/api/access-urls")
def access_urls(user=Depends(authenticate)):
    port = 3249
    addresses = {"127.0.0.1"}
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = result[4][0]
            if isinstance(address, str) and not address.startswith("127."):
                addresses.add(address)
    except socket.gaierror:
        pass
    return [f"http://{address}:{port}" for address in sorted(addresses)]


class ChartInput(BaseModel):
    song: str = Field(min_length=1, max_length=200)
    difficulty: str
    level: float = Field(gt=0, le=20)
    constant: float = Field(gt=0, le=20)

    @field_validator("song")
    @classmethod
    def validate_song(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Song title cannot be empty")
        return value


class ChartImportInput(BaseModel):
    charts: list[ChartInput] = Field(min_length=1, max_length=10000)


class SessionInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    charts: list[int] = Field(min_length=1, max_length=10000)
    kind: str = Field(default="train", pattern="^(train|test)$")
    power: float = Field(default=1.0, gt=0, le=10)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be empty")
        return value


class ScoreInput(BaseModel):
    score: int = Field(ge=0, le=1000000)


class TestJudgementInput(BaseModel):
    total_notes: int = Field(ge=0)
    perfect: int = Field(ge=0)
    good: int = Field(ge=0)
    bad: int = Field(ge=0)
    miss: int = Field(ge=0)
    max_combo: int = Field(ge=0)


class ImportPayload(BaseModel):
    data: dict


def calculate_single_test_score(data: TestJudgementInput) -> dict[str, float | int]:
    total = data.total_notes
    judgement_count = data.perfect + data.good + data.bad + data.miss
    if total == 0:
        raise ValueError("Note 总数 N 不能为 0")
    if judgement_count != total:
        raise ValueError("Judgement counts must satisfy N = P + G + B + M")
    if data.max_combo > total:
        raise ValueError("MaxCombo 不能大于 Note 总数")
    accuracy = (data.perfect + 0.65 * data.good) / total * 100
    judgement_score = 900000 * (data.perfect + 0.65 * data.good) / total
    combo_score = 100000 * data.max_combo / total
    return {
        "accuracy": accuracy,
        "score": judgement_score + combo_score,
        "judgement_score": judgement_score,
        "combo_score": combo_score,
    }


@app.get("/", response_class=FileResponse)
def home():
    return FileResponse(STATIC_INDEX)


@app.get("/api/charts")
def get_charts(user=Depends(authenticate)):
    with connect() as db:
        rows = db.execute("""
            SELECT charts.id, songs.name AS song_name, charts.difficulty,
                   charts.level, charts.constant
            FROM charts JOIN songs ON songs.id=charts.song_id
            ORDER BY songs.name, charts.difficulty
        """).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/charts")
def add_chart(data: ChartInput, user=Depends(authenticate)):
    difficulty = data.difficulty.upper()
    if difficulty not in {"EZ", "HD", "IN", "AT"}:
        raise HTTPException(400, "Difficulty must be EZ, HD, IN, or AT")
    try:
        with database() as db:
            db.execute("INSERT OR IGNORE INTO songs(name) VALUES(?)", (data.song,))
            song = db.execute("SELECT id FROM songs WHERE name=?", (data.song,)).fetchone()
            db.execute("INSERT INTO charts(song_id,difficulty,level,constant) VALUES(?,?,?,?)",
                       (song["id"], difficulty, data.level, data.constant))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This chart already exists")
    return {"message": "添加成功"}


@app.post("/api/charts/import")
def import_charts(payload: ChartImportInput, user=Depends(authenticate)):
    added = skipped = 0
    with database() as db:
        for chart in payload.charts:
            difficulty = chart.difficulty.upper()
            if difficulty not in {"EZ", "HD", "IN", "AT"}:
                raise HTTPException(400, "Difficulty must be EZ, HD, IN, or AT")
            db.execute("INSERT OR IGNORE INTO songs(name) VALUES(?)", (chart.song,))
            song = db.execute("SELECT id FROM songs WHERE name=?", (chart.song,)).fetchone()
            cursor = db.execute(
                "INSERT OR IGNORE INTO charts(song_id,difficulty,level,constant) VALUES(?,?,?,?)",
                (song["id"], difficulty, chart.level, chart.constant),
            )
            if cursor.rowcount:
                added += 1
            else:
                skipped += 1
    return {"added": added, "skipped": skipped,
            "message": f"已导入 {added} 张谱面，跳过 {skipped} 张重复谱面"}


@app.put("/api/charts/{chart_id}")
def update_chart(chart_id: int, data: ChartInput, user=Depends(authenticate)):
    difficulty = data.difficulty.upper()
    if difficulty not in {"EZ", "HD", "IN", "AT"}:
        raise HTTPException(400, "Difficulty must be EZ, HD, IN, or AT")
    try:
        with database() as db:
            chart = db.execute("SELECT song_id FROM charts WHERE id=?", (chart_id,)).fetchone()
            if not chart:
                raise HTTPException(404, "Chart not found")
            db.execute("INSERT OR IGNORE INTO songs(name) VALUES(?)", (data.song,))
            song = db.execute("SELECT id FROM songs WHERE name=?", (data.song,)).fetchone()
            db.execute("""
                UPDATE charts SET song_id=?,difficulty=?,level=?,constant=? WHERE id=?
            """, (song["id"], difficulty, data.level, data.constant, chart_id))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This chart already exists")
    return {"message": "Chart updated"}


def insert_session(data: SessionInput, user_id: int) -> int:
    with database() as db:
        for chart_id in data.charts:
            if not db.execute("SELECT id FROM charts WHERE id=?", (chart_id,)).fetchone():
                raise HTTPException(404, "Chart not found")
        cursor = db.execute(
            "INSERT INTO sessions(name,created_at,kind,power,user_id) VALUES(?,?,?,?,?)",
            (data.name, datetime.now().isoformat(), data.kind, data.power, user_id),
        )
        session_id = cursor.lastrowid
        if session_id is None:
            raise RuntimeError("No ID was returned after creating the record")
        for sequence, chart_id in enumerate(data.charts, start=1):
            db.execute("INSERT INTO session_charts(session_id,chart_id,sequence) VALUES(?,?,?)",
                       (session_id, chart_id, sequence))
    return session_id


@app.post("/api/sessions")
def create_session(data: SessionInput, user=Depends(authenticate)):
    data.kind = "train"
    return {"id": insert_session(data, user["id"]), "message": "Training session created"}


@app.post("/api/tests")
def create_test(data: SessionInput, user=Depends(authenticate)):
    data.kind = "test"
    return {"id": insert_session(data, user["id"]), "message": "Test session created"}


@app.get("/api/sessions")
def get_sessions(user=Depends(authenticate)):
    with connect() as db:
        rows = db.execute("""
            SELECT s.id,s.name,s.created_at,s.kind,s.power,COUNT(sc.id) AS chart_count,
                   COUNT(r.id) AS recorded_count,COALESCE(SUM(r.score),0) AS total_score,
                   COALESCE(AVG(r.accuracy),0) AS average_accuracy
            FROM sessions s LEFT JOIN session_charts sc ON sc.session_id=s.id
            LEFT JOIN records r ON r.session_chart_id=sc.id
            WHERE s.user_id=? GROUP BY s.id ORDER BY s.id DESC
        """, (user["id"],)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/sessions/{session_id}")
def get_session(session_id: int, user=Depends(authenticate)):
    with connect() as db:
        session = db.execute("SELECT * FROM sessions WHERE id=? AND user_id=?",
                      (session_id, user["id"])).fetchone()
        if not session:
            raise HTTPException(404, "Record not found")
        rows = db.execute("""
            SELECT sc.id AS session_chart_id,sc.sequence,songs.name AS song_name,
                   charts.difficulty,charts.level,charts.constant,records.score,records.accuracy
            FROM session_charts sc JOIN charts ON charts.id=sc.chart_id
            JOIN songs ON songs.id=charts.song_id
            LEFT JOIN records ON records.session_chart_id=sc.id
            WHERE sc.session_id=? ORDER BY sc.sequence
        """, (session_id,)).fetchall()
    return {"session": dict(session), "charts": [dict(row) for row in rows]}


@app.put("/api/sessions/{session_id}/records/{record_id}")
def save_score(session_id: int, record_id: int, data: ScoreInput,
               user=Depends(authenticate)):
    with database() as db:
        row = db.execute("""
            SELECT sc.id FROM session_charts sc JOIN sessions s ON s.id=sc.session_id
            WHERE sc.id=? AND sc.session_id=? AND s.kind='train' AND s.user_id=?
        """, (record_id, session_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Training chart not found")
        db.execute("""
            INSERT INTO records(session_chart_id,score,played_at,accuracy) VALUES(?,?,?,NULL)
            ON CONFLICT(session_chart_id) DO UPDATE SET score=excluded.score,
            accuracy=NULL,played_at=excluded.played_at
        """, (record_id, data.score, datetime.now().isoformat()))
    return {"message": "Score saved"}


@app.put("/api/tests/{session_id}/records/{record_id}")
def save_test_judgement(session_id: int, record_id: int, data: TestJudgementInput,
                        user=Depends(authenticate)):
    try:
        calculation = calculate_single_test_score(data)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    with database() as db:
        row = db.execute("""
            SELECT sc.id FROM session_charts sc JOIN sessions s ON s.id=sc.session_id
            WHERE sc.id=? AND sc.session_id=? AND s.kind='test' AND s.user_id=?
        """, (record_id, session_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Test chart not found")
        db.execute("""
            INSERT INTO records(session_chart_id,score,played_at,accuracy) VALUES(?,?,?,?)
            ON CONFLICT(session_chart_id) DO UPDATE SET score=excluded.score,
            accuracy=excluded.accuracy,played_at=excluded.played_at
        """, (record_id, calculation["score"], datetime.now().isoformat(), calculation["accuracy"]))
    return {"message": "Test judgement saved", "accuracy": calculation["accuracy"],
            "score": calculation["score"]}


def calculate_test_result(db: sqlite3.Connection, session_id: int, user_id: int) -> dict:
    session = db.execute("SELECT power FROM sessions WHERE id=? AND kind='test' AND user_id=?",
                         (session_id, user_id)).fetchone()
    if not session:
        raise HTTPException(404, "Test not found")
    rows = db.execute("""
        SELECT songs.name AS chart_name,charts.level,records.accuracy
        FROM records JOIN session_charts ON session_charts.id=records.session_chart_id
        JOIN charts ON charts.id=session_charts.chart_id
        JOIN songs ON songs.id=charts.song_id
        WHERE session_charts.session_id=? AND records.accuracy IS NOT NULL
    """, (session_id,)).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["chart_name"]] = counts.get(row["chart_name"], 0) + 1
    weighted_sum = 0.0
    weight_sum = 0.0
    power = float(session["power"])
    for row in rows:
        weight = float(row["level"]) ** power / math.sqrt(counts[row["chart_name"]])
        weighted_sum += weight * float(row["accuracy"])
        weight_sum += weight
    unique_count = len(counts)
    return {
        "score": weighted_sum / weight_sum if weight_sum else None,
        "record_count": len(rows),
        "unique_chart_count": unique_count,
        "is_official": unique_count >= MIN_UNIQUE_CHARTS,
        "power": power,
    }


@app.get("/api/tests/{session_id}/result")
def test_result(session_id: int, user=Depends(authenticate)):
    with connect() as db:
        return calculate_test_result(db, session_id, user["id"])


@app.get("/api/analytics/chart/{chart_id}")
def chart_history(chart_id: int, user=Depends(authenticate)):
    with connect() as db:
        rows = db.execute("""
            SELECT records.played_at,sessions.created_at,records.score,records.accuracy,
                   sessions.kind,session_charts.sequence
            FROM records JOIN session_charts ON session_charts.id=records.session_chart_id
            JOIN sessions ON sessions.id=session_charts.session_id
            WHERE session_charts.chart_id=? AND sessions.user_id=?
            ORDER BY records.played_at,sessions.id,session_charts.sequence
        """, (chart_id, user["id"])).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/analytics/summary")
def summary(user=Depends(authenticate)):
    with connect() as db:
        row = db.execute("""
            SELECT COUNT(DISTINCT sessions.id) AS sessions,COUNT(records.id) AS plays,
                   COALESCE(MAX(CASE WHEN sessions.kind='train' THEN records.score END),0) AS best_score,
                   COALESCE(AVG(CASE WHEN sessions.kind='train' THEN records.score END),0) AS average_score
            FROM sessions LEFT JOIN session_charts ON sessions.id=session_charts.session_id
            LEFT JOIN records ON records.session_chart_id=session_charts.id
            WHERE sessions.user_id=?
        """, (user["id"],)).fetchone()
        history = db.execute("""
             SELECT sessions.id AS session_id,sessions.name,sessions.kind,
                 session_charts.id AS session_chart_id,session_charts.sequence,
                 records.score,records.accuracy,records.played_at
             FROM records JOIN session_charts ON session_charts.id=records.session_chart_id
             JOIN sessions ON sessions.id=session_charts.session_id
             WHERE sessions.user_id=?
             ORDER BY records.played_at,sessions.id,session_charts.sequence
        """, (user["id"],)).fetchall()
    return {"summary": dict(row), "history": [dict(item) for item in history]}


@app.get("/api/data/export")
def export_data(user=Depends(authenticate)):
    with connect() as db:
        sessions = db.execute("""
            SELECT id,name,created_at,kind,power
            FROM sessions WHERE user_id=? ORDER BY id
        """, (user["id"],)).fetchall()
        charts = db.execute("""
            SELECT DISTINCT songs.name AS song,charts.difficulty,charts.level,charts.constant
            FROM charts JOIN songs ON songs.id=charts.song_id
            JOIN session_charts ON session_charts.chart_id=charts.id
            JOIN sessions ON sessions.id=session_charts.session_id
            WHERE sessions.user_id=? ORDER BY songs.name,charts.difficulty
        """, (user["id"],)).fetchall()
        session_data = []
        for session in sessions:
            chart_rows = db.execute("""
                SELECT songs.name AS song,charts.difficulty,charts.level,charts.constant,
                       sc.sequence,records.score,records.accuracy,records.played_at
                FROM session_charts sc JOIN charts ON charts.id=sc.chart_id
                JOIN songs ON songs.id=charts.song_id
                LEFT JOIN records ON records.session_chart_id=sc.id
                WHERE sc.session_id=? ORDER BY sc.sequence
            """, (session["id"],)).fetchall()
            session_data.append({
                "name": session["name"], "created_at": session["created_at"],
                "kind": session["kind"], "power": session["power"],
                "charts": [dict(row) for row in chart_rows],
            })
    payload = {"format": "phitest-export", "version": 1,
               "exported_at": datetime.now().isoformat(),
               "charts": [dict(row) for row in charts], "sessions": session_data}
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(content=content, media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=phitest-export.json"})


def import_export_data(data: dict, user_id: int) -> dict[str, int]:
    if data.get("format") != "phitest-export" or data.get("version") != 1:
        raise HTTPException(400, "不支持的导出文件格式或版本")
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        raise HTTPException(400, "导出文件缺少有效的场次数据")
    chart_count = session_count = record_count = 0
    with database() as db:
        chart_ids = {}
        for chart in data.get("charts", []):
            if not isinstance(chart, dict) or not isinstance(chart.get("song"), str) or not isinstance(chart.get("difficulty"), str):
                raise HTTPException(400, "导出文件包含无效谱面")
            song = chart["song"].strip()
            difficulty = chart["difficulty"].upper()
            if not song or difficulty not in {"EZ", "HD", "IN", "AT"}:
                raise HTTPException(400, "导出文件包含无效谱面")
            db.execute("INSERT OR IGNORE INTO songs(name) VALUES(?)", (song,))
            song_row = db.execute("SELECT id FROM songs WHERE name=?", (song,)).fetchone()
            db.execute("""
                INSERT OR IGNORE INTO charts(song_id,difficulty,level,constant) VALUES(?,?,?,?)
            """, (song_row["id"], difficulty, chart.get("level"), chart.get("constant")))
            chart_row = db.execute("SELECT id FROM charts WHERE song_id=? AND difficulty=?",
                                   (song_row["id"], difficulty)).fetchone()
            chart_ids[(song, difficulty)] = chart_row["id"]
            chart_count += 1
        for session in sessions:
            if not isinstance(session, dict) or not isinstance(session.get("name"), str):
                raise HTTPException(400, "导出文件包含无效场次")
            kind = session.get("kind", "train")
            power = session.get("power", 1.0)
            if kind not in {"train", "test"} or not isinstance(power, (int, float)) or power <= 0:
                raise HTTPException(400, "导出文件包含无效场次")
            chart_rows = session.get("charts")
            if not isinstance(chart_rows, list) or not chart_rows:
                raise HTTPException(400, "场次必须包含谱面")
            cursor = db.execute("""
                INSERT INTO sessions(name,created_at,kind,power,user_id) VALUES(?,?,?,?,?)
            """, (session["name"].strip(), session.get("created_at", datetime.now().isoformat()),
                  kind, power, user_id))
            new_session_id = cursor.lastrowid
            session_count += 1
            for fallback_sequence, item in enumerate(chart_rows, start=1):
                key = (str(item.get("song", "")).strip(), str(item.get("difficulty", "")).upper())
                chart_id = chart_ids.get(key)
                if chart_id is None:
                    raise HTTPException(400, "场次引用了导出文件中不存在的谱面")
                sequence = item.get("sequence", fallback_sequence)
                sc_cursor = db.execute("""
                    INSERT INTO session_charts(session_id,chart_id,sequence) VALUES(?,?,?)
                """, (new_session_id, chart_id, sequence))
                session_chart_id = sc_cursor.lastrowid
                if item.get("score") is not None:
                    score = item["score"]
                    accuracy = item.get("accuracy")
                    if not isinstance(score, (int, float)) or not 0 <= score <= 1000000:
                        raise HTTPException(400, "导出文件包含无效成绩")
                    db.execute("""
                        INSERT INTO records(session_chart_id,score,played_at,accuracy) VALUES(?,?,?,?)
                    """, (session_chart_id, score, item.get("played_at", datetime.now().isoformat()), accuracy))
                    record_count += 1
    return {"charts": chart_count, "sessions": session_count, "records": record_count}


@app.post("/api/data/import")
def import_data(payload: ImportPayload, user=Depends(authenticate)):
    result = import_export_data(payload.data, user["id"])
    return {"message": f"已导入 {result['sessions']} 个场次和 {result['records']} 条成绩", **result}


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=3249)
