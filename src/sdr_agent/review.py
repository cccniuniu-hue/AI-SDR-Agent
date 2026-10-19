from pydantic import ValidationError

from .repository import Repository, encode, now
from .schemas import Lead


class ReviewError(ValueError):
    pass


class ReviewService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def apply_batch(self, changes: list[dict], reviewer: str, reason: str):
        if not reviewer.strip() or not reason.strip():
            raise ReviewError("请填写审核人和修正/核实说明")
        if not changes or len({c["run_id"] for c in changes}) != len(changes):
            raise ReviewError("请选择不同的记录进行处理")
        with self.repository.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for change in changes:
                row = conn.execute(
                    "SELECT * FROM analysis_runs WHERE id=?", (change["run_id"],)
                ).fetchone()
                if row is None or row["run_status"] not in ("succeeded", "partial"):
                    raise ReviewError("不存在或失败的记录不能审核")
                if row["version"] != change["expected_version"]:
                    raise ReviewError("记录已被其他页面修改，请刷新后重试；本批修改未保存")
                original = Lead.model_validate_json(row["current_json"]).model_dump()
                updates = change.get("updates", {})
                if any(
                    k not in Lead.model_fields or k in ("confidence_score", "need_human_review")
                    for k in updates
                ):
                    raise ReviewError("不能手工修改系统质量分或审核标记")
                merged = {**original, **updates}
                if (
                    "intent_level" in updates
                    and updates["intent_level"] != original["intent_level"]
                    and "intent_reason" not in updates
                ):
                    merged["intent_reason"] = "人工复核：" + reason.strip()
                if not isinstance(change.get("approve", False), bool):
                    raise ReviewError("审核动作必须为布尔值")
                approve = change.get("approve", False)
                merged["need_human_review"] = not approve
                try:
                    after = Lead.model_validate(merged).model_dump(mode="json")
                except ValidationError as exc:
                    raise ReviewError("修改字段格式不合法，请检查人数、意向等级和通话时间") from exc
                unknown = [name for name in ("company_name", "intent_level") if after[name] is None]
                reason_text = reason.strip()
                if approve and unknown:
                    if change.get("accept_unknown") is not True:
                        raise ReviewError("企业或意向仍未知，请明确确认已检查并接受未知值")
                    reason_text += (
                        "；已检查，现有信息无法确认 " + "、".join(unknown) + "，人工接受 null。"
                    )
                next_version = row["version"] + 1
                conn.execute(
                    "UPDATE analysis_runs SET current_json=?, review_status=?, version=? WHERE id=? AND version=?",
                    (
                        encode(after),
                        "approved" if approve else "edited",
                        next_version,
                        row["id"],
                        row["version"],
                    ),
                )
                conn.execute(
                    "INSERT INTO review_events(run_id,before_json,after_json,reviewer,reason,action,from_version,to_version,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        row["current_json"],
                        encode(after),
                        reviewer.strip(),
                        reason_text,
                        "approve" if approve else "edit",
                        row["version"],
                        next_version,
                        now(),
                    ),
                )
