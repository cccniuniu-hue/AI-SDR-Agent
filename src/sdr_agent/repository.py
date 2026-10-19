import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .schemas import AnalysisInput, AnalysisResult, Lead


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(data) -> str:
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, transcript TEXT NOT NULL, call_time TEXT,
                    created_at TEXT NOT NULL, content_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                    run_status TEXT NOT NULL, original_json TEXT, current_json TEXT,
                    result_json TEXT NOT NULL, review_status TEXT NOT NULL,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS review_events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES analysis_runs(id),
                    before_json TEXT NOT NULL, after_json TEXT NOT NULL, reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL, action TEXT NOT NULL, from_version INTEGER NOT NULL,
                    to_version INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS company_aliases (
                    id INTEGER PRIMARY KEY, alias TEXT NOT NULL, company_name TEXT NOT NULL,
                    area TEXT NOT NULL, industry TEXT NOT NULL, source TEXT NOT NULL,
                    reviewer TEXT NOT NULL, relation TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save_run(self, input: AnalysisInput, result: AnalysisResult):
        lead = result.lead.model_dump(mode="json") if result.lead else None
        call_id, timestamp = str(uuid.uuid4()), now()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO calls VALUES (?,?,?,?,?)",
                (
                    call_id,
                    input.transcript,
                    input.call_time,
                    timestamp,
                    hashlib.sha256(input.transcript.encode()).hexdigest(),
                ),
            )
            conn.execute(
                "INSERT INTO analysis_runs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    result.run_id,
                    call_id,
                    result.run_status,
                    encode(lead),
                    encode(lead),
                    result.model_dump_json(),
                    "pending",
                    1,
                    timestamp,
                ),
            )

    @staticmethod
    def _decode(row):
        if row is None:
            raise KeyError("找不到该分析记录")
        data = dict(row)
        data["run_id"] = data.pop("id")
        data["original"] = json.loads(data.pop("original_json"))
        data["current"] = json.loads(data.pop("current_json"))
        data["analysis"] = json.loads(data.pop("result_json"))
        return data

    def get_run(self, run_id: str):
        with self.connection() as conn:
            return self._decode(
                conn.execute(
                    "SELECT r.*, c.transcript, c.call_time FROM analysis_runs r JOIN calls c ON c.id=r.call_id WHERE r.id=?",
                    (run_id,),
                ).fetchone()
            )

    def list_runs(self):
        with self.connection() as conn:
            return [
                self._decode(row)
                for row in conn.execute(
                    "SELECT r.*, c.transcript, c.call_time FROM analysis_runs r JOIN calls c ON c.id=r.call_id ORDER BY r.created_at DESC"
                )
            ]

    def history(self, run_id: str):
        with self.connection() as conn:
            records = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM review_events WHERE run_id=? ORDER BY id DESC", (run_id,)
                )
            ]
        for row in records:
            row["before"] = json.loads(row.pop("before_json"))
            row["after"] = json.loads(row.pop("after_json"))
        return records

    def export(self, run_ids: list[str]):
        # One read transaction: all exported records share a consistent snapshot.
        with self.connection() as conn:
            conn.execute("BEGIN")
            result = []
            for run_id in run_ids:
                row = conn.execute(
                    "SELECT current_json, run_status FROM analysis_runs WHERE id=?", (run_id,)
                ).fetchone()
                if row is None or row["run_status"] not in ("succeeded", "partial"):
                    raise ValueError("失败或不存在的记录不可导出")
                result.append(Lead.model_validate_json(row["current_json"]).model_dump(mode="json"))
            return result

    def aliases(self):
        with self.connection() as conn:
            return [
                dict(row) for row in conn.execute("SELECT * FROM company_aliases ORDER BY id DESC")
            ]

    def save_alias(self, *, alias, company_name, area, industry, source, reviewer, relation):
        values = [
            str(x).strip() if x is not None else ""
            for x in (alias, company_name, area, industry, source, reviewer, relation)
        ]
        if not all(values):
            raise ValueError("别名、主体、地区、行业、来源、审核人和主体关系均需填写")
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO company_aliases(alias,company_name,area,industry,source,reviewer,relation,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (*values, now()),
            )
