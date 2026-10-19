"""Isolated synthetic records and a local API simulator for manual acceptance."""

import argparse
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sdr_agent.repository import Repository
from sdr_agent.schemas import AnalysisInput, AnalysisResult, Lead

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = "客户：我们叫测试木作，在杭州，做全屋定制。"
SCENARIOS = (
    "ok",
    "401",
    "402",
    "403",
    "404",
    "429",
    "503",
    "retry-once",
    "malformed-once",
    "malformed-always",
    "search-failure",
    "timeout",
)


def seed(database):
    path = Path(database).resolve()
    allowed = (ROOT / ".data" / "manual-tests").resolve()
    if not path.is_relative_to(allowed):
        raise ValueError("测试库必须位于本项目 .data/manual-tests/ 内")
    if path.exists():
        raise ValueError("文件已存在，请换一个测试库文件名；不会覆盖已有数据")
    repo = Repository(path)
    rows = [
        ("manual-high", "high", 1, "succeeded"),
        ("manual-medium", "medium", 8, "succeeded"),
        ("manual-low", "low", 31, "succeeded"),
        ("manual-unknown", None, 1, "succeeded"),
        ("manual-failed", None, 1, "failed"),
    ]
    for run_id, intent, days, status in rows:
        lead = Lead(
            company_name="手工验收合成主体有限公司" if intent else None,
            industry="全屋定制",
            area="杭州",
            contact_name="合成测试联系人",
            intent_level=intent,
            intent_reason="人工预置的测试标签，不是模型判断。",
            demand_summary="仅用于审核、筛选和导出操作验收。",
            mendian=2,
            shejishi=12,
            call_time="2026-10-18T06:30:45.123456+00:00",
            confidence_score=0.5,
            need_human_review=True,
        )
        repo.save_run(
            AnalysisInput(transcript="客户：这是手工验收用的合成记录，不代表真实客户。"),
            AnalysisResult(
                run_id=run_id,
                run_status=status,
                lead=lead if status != "failed" else None,
                error_code="timeout" if status == "failed" else None,
                versions={"model": "manual-fixture", "prompt": "synthetic"},
                review_reasons=["人工预置数据，仅验证界面和存储流程。"],
            ),
        )
        created = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with repo.connection() as conn:
            conn.execute("UPDATE analysis_runs SET created_at=? WHERE id=?", (created, run_id))
            conn.execute(
                "UPDATE calls SET created_at=? WHERE id=(SELECT call_id FROM analysis_runs WHERE id=?)",
                (created, run_id),
            )
    print(f"已创建独立合成测试库：{path}")
    print("5 条记录：high / medium / low / unknown / failed；未调用模型。")


def serve(scenario, port, delay):
    counter = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with lock:
                counter[self.path] = counter.get(self.path, 0) + 1
                count = counter[self.path]
            print(f"{scenario} {self.path} request={count}", flush=True)
            status, payload = 200, {}
            if scenario.isdigit():
                status = int(scenario)
            elif scenario == "retry-once" and count == 1:
                status = 429
            elif scenario == "search-failure" and self.path.endswith("/tools/search"):
                status = 503
            elif scenario == "timeout":
                time.sleep(delay)
            if status != 200:
                payload = {"error": {"message": "Synthetic manual-test error"}}
            elif self.path.endswith("/chat/completions"):
                extraction = {"facts": {}}
                if scenario == "search-failure":
                    quote = SAMPLE.removeprefix("客户：")
                    evidence = [{"turn_id": 1, "speaker": "customer", "quote": quote}]
                    extraction = {
                        "company_mention": "测试木作",
                        "company_evidence": evidence,
                        "facts": {
                            "area": {"value": "杭州", "evidence": evidence},
                            "industry": {"value": "全屋定制", "evidence": evidence},
                        },
                    }
                content = json.dumps(extraction, ensure_ascii=False)
                if scenario == "malformed-always" or (scenario == "malformed-once" and count == 1):
                    content = "{invalid-json"
                payload = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
            elif self.path.endswith("/tools/search"):
                payload = {"results": []}
            else:
                status = 404
                payload = {"error": {"message": "Unknown simulator endpoint"}}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    print(f"合成服务：http://127.0.0.1:{port}，场景 {scenario}。不会调用外部模型。", flush=True)
    print("每个场景重新启动模拟器，计数从 1 开始；Ctrl+C 停止。", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed_parser = commands.add_parser("seed")
    seed_parser.add_argument("--database", required=True)
    api_parser = commands.add_parser("serve")
    api_parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    api_parser.add_argument("--port", type=int, default=8877)
    api_parser.add_argument("--delay", type=float, default=35)
    args = parser.parse_args()
    if args.command == "seed":
        seed(args.database)
    else:
        serve(args.scenario, args.port, args.delay)


if __name__ == "__main__":
    main()
