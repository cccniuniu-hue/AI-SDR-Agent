from .schemas import Extraction, IntentDecision, Signal

WEIGHTS = {"pain": 30, "timeline": 25, "budget": 20, "engagement": 20, "scale": 5}
LABELS = {
    "pain": "痛点",
    "timeline": "时间",
    "budget": "预算",
    "engagement": "行动",
    "scale": "规模",
}
ACTIONS = {
    "high": "确认决策人与试用时间，安排演示并提供针对性方案。",
    "medium": "补充确认预算、时间安排和决策流程，再约定下一次沟通。",
    "low": "尊重客户当前意愿，记录原因；如客户同意，再安排后续联系。",
    None: "人工核对原文，补充企业主体、实际需求及采购计划。",
}


def format_intent_reason(reason: str, signals: dict[str, Signal]) -> str:
    details = [
        f"{LABELS[k]}：" + signal.evidence[0].quote.rstrip("。；; \t\r\n")
        for k, signal in signals.items()
        if signal.value is not None and signal.evidence
    ]
    missing = [LABELS[k] for k, signal in signals.items() if signal.value is None]
    if details:
        reason += "\n依据：\n" + "；\n".join(details)
    if missing:
        reason += "\n未知：" + "、".join(missing) + "。"
    return reason


def score_intent(extraction: Extraction) -> IntentDecision:
    signals = {name: extraction.signals.get(name, Signal()) for name in WEIGHTS}
    known = {k: s.value for k, s in signals.items() if s.value is not None and not s.conflict}
    lower = sum(WEIGHTS[k] * value for k, value in known.items()) if known else None
    weight = sum(WEIGHTS[k] for k in known)
    upper = lower + 100 - weight if lower is not None else None
    conflict = (
        extraction.conflicted
        or extraction.role_uncertain
        or any(s.conflict for s in signals.values())
    )
    level = None
    evidence_score = weight / 100 if known else None
    if conflict:
        reason = "对话存在角色或关键信号冲突，需要人工判断。"
        evidence_score = None
    elif extraction.refusal and extraction.refusal_evidence:
        level, evidence_score = "low", 0.9
        reason = "客户明确拒绝当前采购推进：" + "；".join(
            e.quote for e in extraction.refusal_evidence
        )
    elif (
        lower is not None
        and lower >= 70
        and known.get("pain") == 1
        and max(known.get("timeline", 0), known.get("budget", 0)) >= 0.5
        and known.get("engagement", 0) >= 0.5
    ):
        level, reason = "high", "明确痛点、时间或预算及推进信号支持高意向。"
    elif weight >= 60 and upper is not None and upper < 40:
        level, reason = "low", "已知信号主要为明确的低需求或缺少采购计划。"
    elif (
        sum(v > 0 for v in known.values()) >= 2
        and max(known.get("pain", 0), known.get("engagement", 0)) > 0
    ):
        level, reason = "medium", "已有多项正向需求信号，但不足以确认近期采购。"
    else:
        reason = "证据不足，不能将信息缺失等同于低意向。"
    return IntentDecision(
        level=level,
        reason=format_intent_reason(reason, signals),
        signals=signals,
        score_lower=lower,
        score_upper=upper,
        evidence_score=evidence_score,
        next_action=ACTIONS[level],
    )
