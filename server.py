import math
import os
import secrets
import socket
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path

import uvicorn

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "phitest.db"
STATIC_INDEX = BASE_DIR / "static" / "index.html"
MIN_UNIQUE_CHARTS = 3
configured_username = os.getenv("PHITEST_USER")
configured_password = os.getenv("PHITEST_PASSWORD")
if not configured_username or not configured_password:
    raise RuntimeError("缺少 PHITEST_USER 或 PHITEST_PASSWORD 环境变量")
USERNAME: str = configured_username
PASSWORD: str = configured_password

security = HTTPBasic()


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
            power REAL NOT NULL DEFAULT 1.0
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
        """)
        session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        if "kind" not in session_columns:
            db.execute("ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'train'")
        if "power" not in session_columns:
            db.execute("ALTER TABLE sessions ADD COLUMN power REAL NOT NULL DEFAULT 1.0")
        record_columns = {row[1] for row in db.execute("PRAGMA table_info(records)")}
        if "accuracy" not in record_columns:
            db.execute("ALTER TABLE records ADD COLUMN accuracy REAL")
        db.execute("PRAGMA journal_mode = WAL")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
 

app = FastAPI(title="PhiTest Training", lifespan=lifespan)


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    user_ok = secrets.compare_digest(credentials.username, USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, PASSWORD)
    if not user_ok or not pass_ok:
        raise HTTPException(401, "用户名或密码错误", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


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
            raise ValueError("曲名不能为空")
        return value


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
            raise ValueError("名称不能为空")
        return value


class ScoreInput(BaseModel):
    score: int = Field(ge=0, le=1000000)


class AccuracyInput(BaseModel):
    accuracy: float = Field(ge=0, le=100)


@app.get("/", response_class=FileResponse)
def home(user=Depends(authenticate)):
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
        raise HTTPException(400, "难度必须为 EZ、HD、IN 或 AT")
    try:
        with database() as db:
            db.execute("INSERT OR IGNORE INTO songs(name) VALUES(?)", (data.song,))
            song = db.execute("SELECT id FROM songs WHERE name=?", (data.song,)).fetchone()
            db.execute("INSERT INTO charts(song_id,difficulty,level,constant) VALUES(?,?,?,?)",
                       (song["id"], difficulty, data.level, data.constant))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该谱面已经存在")
    return {"message": "添加成功"}


def insert_session(data: SessionInput) -> int:
    with database() as db:
        for chart_id in data.charts:
            if not db.execute("SELECT id FROM charts WHERE id=?", (chart_id,)).fetchone():
                raise HTTPException(404, "谱面不存在")
        cursor = db.execute(
            "INSERT INTO sessions(name,created_at,kind,power) VALUES(?,?,?,?)",
            (data.name, datetime.now().isoformat(), data.kind, data.power),
        )
        session_id = cursor.lastrowid
        if session_id is None:
            raise RuntimeError("创建记录后没有返回 ID")
        for sequence, chart_id in enumerate(data.charts, start=1):
            db.execute("INSERT INTO session_charts(session_id,chart_id,sequence) VALUES(?,?,?)",
                       (session_id, chart_id, sequence))
    return session_id


@app.post("/api/sessions")
def create_session(data: SessionInput, user=Depends(authenticate)):
    data.kind = "train"
    return {"id": insert_session(data), "message": "训练创建成功"}


@app.post("/api/tests")
def create_test(data: SessionInput, user=Depends(authenticate)):
    data.kind = "test"
    return {"id": insert_session(data), "message": "测试创建成功"}


@app.get("/api/sessions")
def get_sessions(user=Depends(authenticate)):
    with connect() as db:
        rows = db.execute("""
            SELECT s.id,s.name,s.created_at,s.kind,s.power,COUNT(sc.id) AS chart_count,
                   COUNT(r.id) AS recorded_count,COALESCE(SUM(r.score),0) AS total_score,
                   COALESCE(AVG(r.accuracy),0) AS average_accuracy
            FROM sessions s LEFT JOIN session_charts sc ON sc.session_id=s.id
            LEFT JOIN records r ON r.session_chart_id=sc.id
            GROUP BY s.id ORDER BY s.id DESC
        """).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/sessions/{session_id}")
def get_session(session_id: int, user=Depends(authenticate)):
    with connect() as db:
        session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(404, "记录不存在")
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
            WHERE sc.id=? AND sc.session_id=? AND s.kind='train'
        """, (record_id, session_id)).fetchone()
        if not row:
            raise HTTPException(404, "训练曲目不存在")
        db.execute("""
            INSERT INTO records(session_chart_id,score,played_at,accuracy) VALUES(?,?,?,NULL)
            ON CONFLICT(session_chart_id) DO UPDATE SET score=excluded.score,
            accuracy=NULL,played_at=excluded.played_at
        """, (record_id, data.score, datetime.now().isoformat()))
    return {"message": "成绩保存成功"}


@app.put("/api/tests/{session_id}/records/{record_id}")
def save_test_accuracy(session_id: int, record_id: int, data: AccuracyInput,
                       user=Depends(authenticate)):
    with database() as db:
        row = db.execute("""
            SELECT sc.id FROM session_charts sc JOIN sessions s ON s.id=sc.session_id
            WHERE sc.id=? AND sc.session_id=? AND s.kind='test'
        """, (record_id, session_id)).fetchone()
        if not row:
            raise HTTPException(404, "测试谱面不存在")
        db.execute("""
            INSERT INTO records(session_chart_id,score,played_at,accuracy) VALUES(?,?,?,?)
            ON CONFLICT(session_chart_id) DO UPDATE SET score=excluded.score,
            accuracy=excluded.accuracy,played_at=excluded.played_at
        """, (record_id, round(data.accuracy * 10000), datetime.now().isoformat(), data.accuracy))
    return {"message": "测试准确率保存成功"}


def calculate_test_result(db: sqlite3.Connection, session_id: int) -> dict:
    session = db.execute("SELECT power FROM sessions WHERE id=? AND kind='test'",
                         (session_id,)).fetchone()
    if not session:
        raise HTTPException(404, "测试不存在")
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
        return calculate_test_result(db, session_id)


@app.get("/api/analytics/chart/{chart_id}")
def chart_history(chart_id: int, user=Depends(authenticate)):
    with connect() as db:
        rows = db.execute("""
            SELECT records.played_at,sessions.created_at,records.score,records.accuracy,
                   sessions.kind,session_charts.sequence
            FROM records JOIN session_charts ON session_charts.id=records.session_chart_id
            JOIN sessions ON sessions.id=session_charts.session_id
            WHERE session_charts.chart_id=?
            ORDER BY records.played_at,sessions.id,session_charts.sequence
        """, (chart_id,)).fetchall()
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
        """).fetchone()
        history = db.execute("""
             SELECT sessions.id AS session_id,sessions.name,sessions.kind,
                 session_charts.id AS session_chart_id,session_charts.sequence,
                 records.score,records.accuracy,records.played_at
             FROM records JOIN session_charts ON session_charts.id=records.session_chart_id
             JOIN sessions ON sessions.id=session_charts.session_id
             ORDER BY records.played_at,sessions.id,session_charts.sequence
        """).fetchall()
    return {"summary": dict(row), "history": [dict(item) for item in history]}


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=3249)
