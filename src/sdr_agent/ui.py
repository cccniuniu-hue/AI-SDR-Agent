"""Local, text-only analysis and evidence-led review workspace."""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st
from pydantic import ValidationError

from .config import Settings
from .intent import format_intent_reason
from .repository import Repository
from .review import ReviewError, ReviewService
from .schemas import AnalysisInput, Lead, Signal
from .workflow import Workflow

INTENTS = {None: "未知", "high": "高意向", "medium": "中意向", "low": "低意向"}
REVIEWS = {"pending": "未人工审核", "edited": "已修正 · 待审核", "approved": "已审核"}
STATUSES = {
    "succeeded": "分析完成",
    "partial": "部分完成",
    "failed": "分析失败",
    "running": "处理中",
}
FIELDS = {
    "company_name": "企业主体",
    "industry": "行业",
    "area": "地区",
    "contact_name": "联系人",
    "contact_title": "职位",
    "phone": "联系电话",
    "intent_level": "意向等级",
    "intent_reason": "意向依据",
    "demand_summary": "需求摘要",
    "mendian": "门店数量",
    "shejishi": "设计师人数",
    "call_time": "通话时间",
    "next_action": "下一步行动",
    "confidence_score": "置信度",
    "need_human_review": "需人工审核",
}
SIGNALS = {
    "pain": "痛点",
    "timeline": "时间计划",
    "budget": "预算",
    "engagement": "推进意愿",
    "scale": "业务规模",
}
ERRORS = {
    "timeout": "服务响应超时，请稍后重试。",
    "authentication_failed": "模型服务认证失败，请检查本地密钥配置。",
    "missing_api_key": "尚未配置模型密钥，请检查本地 DEEPSEEK_API_KEY。",
    "insufficient_balance": "DeepSeek 账户余额不足，请检查账户后重试。",
    "permission_denied": "当前模型服务权限不足，请检查账户权限。",
    "model_unavailable": "配置的模型不可用，请检查 DEEPSEEK_MODEL。",
    "endpoint_unavailable": "配置的服务端点不可用，请检查服务地址。",
    "request_rejected": "模型服务拒绝了请求，请检查模型和服务配置。",
    "provider_unavailable": "模型服务繁忙、连接失败或请求超时，请稍后重试。",
    "deadline_exceeded": "本次分析已达到总时限，请稍后重试。",
    "invalid_response": "模型未返回符合要求的完整结果，请重试。",
    "invalid_output": "模型输出未通过校验，请检查文本后重试。",
}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def shown(value):
    return "未知 / null" if value is None else str(value)


def timestamp_label(value):
    return (
        datetime.fromisoformat(value)
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def page_heading(title, description):
    st.markdown(
        f'<div class="page-heading"><h1>{escape(title)}</h1><p>{escape(description)}</p></div>',
        unsafe_allow_html=True,
    )


def intent_evidence_text(items, *, detailed=False):
    lines = []
    for item in items or []:
        line = item["quote"]
        if detailed:
            speaker = {"customer": "客户", "sales": "销售", "unknown": "角色不明"}.get(
                item.get("speaker"), "未知"
            )
            line = f"{speaker} · 第{item.get('turn_id', '?')}段：{line}"
        lines.append(line)
    return "\n".join(dict.fromkeys(lines))


def intent_reason_table(current, analysis, *, detailed=False):
    intent = (analysis or {}).get("intent") or {}
    reason = current.get("intent_reason") or "暂无意向判断说明"
    edited = bool(intent) and (
        current.get("intent_reason") != intent.get("reason")
        or current.get("intent_level") != intent.get("level")
    )
    if intent and not edited:
        reason = re.split(r"\s+(?:依据|未知)：", reason, maxsplit=1)[0]
    rows = [("人工核实说明" if edited else "判断说明", "—", reason)]
    if intent:
        for name, label in SIGNALS.items():
            signal = (intent.get("signals") or {}).get(name) or {}
            evidence = intent_evidence_text(signal.get("evidence"), detailed=detailed)
            detail = evidence or "未提供明确依据"
            if signal.get("conflict"):
                detail = "存在冲突，需人工核查。\n" + detail
            elif signal.get("value") is None and evidence:
                detail = "尚无法判断。\n" + detail
            value = "未知" if signal.get("value") is None else str(signal["value"])
            rows.append((label, value, detail))
        extraction = (analysis or {}).get("extraction") or {}
        if extraction.get("refusal") and extraction.get("refusal_evidence"):
            rows.append(
                (
                    "明确拒绝",
                    "—",
                    intent_evidence_text(extraction["refusal_evidence"], detailed=detailed),
                )
            )
    body = "".join(
        f'<tr><th scope="row">{escape(label)}</th>'
        + (f"<td>{escape(value)}</td>" if detailed else "")
        + f"<td>{escape(detail).replace(chr(10), '<br>')}</td></tr>"
        for label, value, detail in rows
    )
    note = (
        '<p class="intent-reason-note">维度行展示原始通话依据；人工修改内容见核实说明。</p>'
        if edited
        else ""
    )
    table_class = "intent-reason-table" + (" intent-signal-table" if detailed else "")
    label = "意向信号" if detailed else "意向依据"
    value_header = '<th scope="col">信号值</th>' if detailed else ""
    return (
        f'<table class="{table_class}" aria-label="{label}">'
        f'<thead><tr><th scope="col">判断维度</th>{value_header}<th scope="col">原文与说明</th></tr></thead>'
        f"<tbody>{body}</tbody></table>{note}"
    )


def render_lead_summary(current, analysis=None):
    groups = [
        (
            "企业与联系人",
            ["company_name", "industry", "area", "contact_name", "contact_title", "phone"],
        ),
        ("业务与通话", ["mendian", "shejishi", "call_time", "need_human_review"]),
    ]
    cards = []
    for title, fields in groups:
        rows = []
        for field in fields:
            value = current[field]
            if field == "need_human_review":
                value = "是" if value else "否"
            rows.append(f"<dt>{FIELDS[field]}</dt><dd>{escape(shown(value))}</dd>")
        cards.append(
            f'<section class="detail-card"><h4>{title}</h4><dl>{"".join(rows)}</dl></section>'
        )
    for field in ("demand_summary", "intent_reason", "next_action"):
        accent = " action" if field == "next_action" else ""
        content = (
            intent_reason_table(current, analysis)
            if field == "intent_reason"
            else f"<p>{escape(shown(current[field]))}</p>"
        )
        cards.append(
            f'<section class="detail-card full{accent}"><h4>{FIELDS[field]}</h4>{content}</section>'
        )
    st.markdown('<div class="detail-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def optional_count(value, label):
    value = value.strip()
    if not value:
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{label}请填写非负整数，未知时留空")
    return int(value)


def call_time_picker(label, *, key, value=None):
    beijing = timezone(timedelta(hours=8))
    initial = (
        datetime.fromisoformat(value)
        .astimezone(beijing)
        .replace(tzinfo=None, second=0, microsecond=0)
        if value is not None
        else None
    )
    # A nullable default lets an existing timestamp be cleared back to null.
    date_key = key + "_date"
    if date_key not in st.session_state:
        st.session_state[date_key] = initial.date() if initial else None
    with st.container(key=key + "_picker"):
        date_col, hour_col, minute_col = st.columns([2.2, 1, 1])
        date = date_col.date_input(
            label,
            value=None,
            min_value=datetime.min.date(),
            max_value=datetime.max.date(),
            key=date_key,
            format="YYYY-MM-DD",
            help="点击日历选择日期，点击 × 清空。日期为空时，通话时间保存为 null。",
        )
        hour = hour_col.selectbox(
            "小时",
            range(24),
            index=initial.hour if initial else 0,
            format_func=lambda value: f"{value:02d}",
            key=key + "_hour",
        )
        minute = minute_col.selectbox(
            "分钟",
            range(60),
            index=initial.minute if initial else 0,
            format_func=lambda value: f"{value:02d}",
            key=key + "_minute",
        )
        st.caption("北京时间（UTC+8）。请核对小时和分钟；日期留空或清空即为未知。")
    if date is None:
        return None
    selected = datetime(date.year, date.month, date.day, hour, minute)
    # Reviewing another field must not discard the original seconds or offset.
    if value is not None and selected == initial:
        return value
    return selected.replace(tzinfo=beijing).isoformat()


def safe_source(url):
    try:
        parsed = urlsplit(url or "")
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def source_link(label, url):
    if safe_source(url):
        st.link_button(label, url)
    elif url:
        st.caption("来源不是可打开的网页地址；请查看证据内容。")


def quotes(items):
    for item in items or []:
        speaker = {"customer": "客户", "sales": "销售", "unknown": "角色不明"}.get(
            item.get("speaker"), "未知"
        )
        label = f"原文 · 第 {item.get('turn_id', '?')} 段 · {speaker}"
        st.markdown(
            f'<blockquote class="evidence-quote"><small>{escape(label)}</small>'
            f"<p>{escape(item.get('quote', ''))}</p></blockquote>",
            unsafe_allow_html=True,
        )


def diff_rows(before, after):
    return [
        {
            "字段": FIELDS.get(field, field),
            "修改前": shown(before.get(field)),
            "修改后": shown(value),
        }
        for field, value in after.items()
        if before.get(field) != value
    ]


def validate_review(changes, runs, reviewer, reason, checked):
    if not reviewer.strip() or not reason.strip():
        raise ValueError("请填写审核人和修正 / 核实说明")
    for change in changes:
        merged = {**runs[change["run_id"]]["current"], **change["updates"]}
        Lead.model_validate(merged)
        if change["approve"] and not checked:
            raise ValueError("标记已审核前，请确认已核查原文和证据")
        if (
            change["approve"]
            and (merged["company_name"] is None or merged["intent_level"] is None)
            and not change["accept_unknown"]
        ):
            raise ValueError("企业或意向仍未知，请明确接受未知值（null）后再审核")


def capture_preview(key, changes, runs, reviewer, reason, checked):
    st.session_state.pop(key, None)
    try:
        validate_review(changes, runs, reviewer, reason, checked)
    except ValidationError:
        st.error("字段格式不合法：请检查人数及带时区的通话时间。")
        return
    except ValueError as exc:
        st.error(str(exc))
        return
    st.session_state[key] = {
        "changes": changes,
        "reviewer": reviewer.strip(),
        "reason": reason.strip(),
        "before": {run_id: runs[run_id]["current"] for run_id in runs},
    }


def render_preview(repo, key, label):
    preview = st.session_state.get(key)
    if not preview:
        return
    with st.container(border=True):
        st.subheader("保存前确认", anchor=False)
        st.info(
            "以下为上次预览时的固定快照。表单有新改动时，请重新点击预览；确认按钮只保存此快照。"
        )
        st.text(f"审核人：{preview['reviewer']}\n说明：{preview['reason']}")
        for change in preview["changes"]:
            st.caption(f"记录 {change['run_id']} · 基于版本 {change['expected_version']}")
            before = preview["before"][change["run_id"]]
            after = {**before, **change["updates"], "need_human_review": not change["approve"]}
            if (
                "intent_level" in change["updates"]
                and change["updates"]["intent_level"] != before["intent_level"]
                and "intent_reason" not in change["updates"]
            ):
                after["intent_reason"] = "人工复核：" + preview["reason"]
            rows = diff_rows(before, after)
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            else:
                st.caption("业务字段无变化，仅记录此次审核操作。")
            st.write("保存后状态：" + ("已审核" if change["approve"] else "已修正 · 待审核"))
            if change["approve"] and change["accept_unknown"]:
                st.caption("已明确接受仍无法确认的企业 / 意向字段为空值。")
        confirm, cancel = st.columns([1, 1])
        if confirm.button(label, type="primary", key=key + "_confirm", width="stretch"):
            try:
                ReviewService(repo).apply_batch(
                    preview["changes"], preview["reviewer"], preview["reason"]
                )
            except ReviewError as exc:
                st.error(str(exc))
            except (ValueError, sqlite3.Error):
                st.error("保存未完成，请刷新记录后重新预览。")
            else:
                del st.session_state[key]
                st.session_state["flash"] = "修改已保存，审核轨迹已记录。"
                st.rerun()
        if cancel.button("取消此预览", key=key + "_cancel", width="stretch"):
            del st.session_state[key]
            st.rerun()


def edit_form(repo, run):
    current, original = run["current"], run["original"] or {}
    company = (run.get("analysis") or {}).get("company") or {}
    company_names = [current["company_name"]] if current["company_name"] else []
    for candidate in company.get("candidates") or []:
        name = (candidate.get("name") or "").strip()
        if name and name not in company_names:
            company_names.append(name)
    company_key = f"review_company_{run['run_id']}_{run['version']}"
    if company_key not in st.session_state:
        st.session_state[company_key] = current["company_name"]
    intent = (run.get("analysis") or {}).get("intent") or {}
    intent_reason = current["intent_reason"] or ""
    if intent.get("signals") and intent_reason == intent.get("reason"):
        intent_reason = format_intent_reason(
            re.split(r"\s+(?:依据|未知)：", intent_reason, maxsplit=1)[0],
            {name: Signal.model_validate(intent["signals"].get(name) or {}) for name in SIGNALS},
        )
    st.caption("空白字段保存为 null。置信度保留原始评估；人工审核状态另行记录。")
    score = original.get("confidence_score")
    st.text(f"原始置信度：{score:.0%}" if score is not None else "原始置信度：未知")
    key = "single_preview_" + run["run_id"]
    with st.form("edit_" + run["run_id"] + "_" + str(run["version"])):
        values = {}
        for title, rows in [
            (
                "企业与联系人",
                [
                    ("company_name",),
                    ("industry", "area"),
                    ("contact_name", "contact_title"),
                    ("phone",),
                ],
            ),
            ("业务与通话", [("mendian", "shejishi"), ("call_time",)]),
        ]:
            st.markdown("##### " + title)
            for row in rows:
                for col, field in zip(st.columns(len(row)), row, strict=True):
                    if field == "company_name":
                        values[field] = col.selectbox(
                            FIELDS[field],
                            company_names,
                            index=None,
                            key=company_key,
                            accept_new_options=True,
                            placeholder="选择候选公司，或输入企业全称",
                            help="选项来自本条线索的企业核实候选，仍需人工核对。输入其他企业全称后按回车；清空后保存为 null。",
                        )
                    elif field == "call_time":
                        with col:
                            values[field] = call_time_picker(
                                "通话日期",
                                value=current[field],
                                key=f"call_time_{run['run_id']}_{run['version']}",
                            )
                    else:
                        values[field] = col.text_input(
                            FIELDS[field],
                            value="" if current[field] is None else str(current[field]),
                        )
        st.markdown("##### 意向与跟进")
        values["intent_level"] = st.selectbox(
            "意向等级",
            list(INTENTS),
            index=list(INTENTS).index(current["intent_level"]),
            format_func=INTENTS.get,
        )
        for field in ("intent_reason", "demand_summary", "next_action"):
            values[field] = st.text_area(
                FIELDS[field],
                value=intent_reason if field == "intent_reason" else current[field] or "",
                height=260 if field == "intent_reason" else 90,
            )
        st.markdown("##### 审核确认")
        reviewer = st.text_input("审核人")
        reason = st.text_area("修正 / 核实说明", height=90)
        approve = st.checkbox("同时标记为已审核")
        checked = st.checkbox("我已核查原文和证据")
        accept_unknown = st.checkbox("已检查并接受企业或意向的未知值（null）")
        submitted = st.form_submit_button("预览本条修改", type="primary")
    if submitted:
        st.session_state.pop(key, None)
        try:
            updates = {
                field: (value.strip() or None) if isinstance(value, str) else value
                for field, value in values.items()
            }
            for field in ("mendian", "shejishi"):
                updates[field] = optional_count(values[field], FIELDS[field])
            updates = {field: value for field, value in updates.items() if value != current[field]}
            changes = [
                {
                    "run_id": run["run_id"],
                    "expected_version": run["version"],
                    "updates": updates,
                    "approve": approve,
                    "accept_unknown": accept_unknown,
                }
            ]
            capture_preview(key, changes, {run["run_id"]: run}, reviewer, reason, checked)
        except ValueError as exc:
            st.error(str(exc))
    render_preview(repo, key, "确认保存本条修改")


def alias_form(repo, run):
    if run["review_status"] != "approved":
        return
    current = run["current"]
    with st.expander("将已核实的简称加入企业别名库"):
        st.caption("别名仅在相同地区和行业中使用。此次保存与线索审核相互独立。")
        with st.form("alias_" + run["run_id"]):
            alias = st.text_input("企业简称 / 别名")
            company = st.text_input("核实后的企业全称", value=current["company_name"] or "")
            left, right = st.columns(2)
            area = left.text_input("适用地区", value=current["area"] or "")
            industry = right.text_input("适用行业", value=current["industry"] or "")
            source = st.text_input("核实来源", placeholder="来源链接或可追溯的人工核验记录")
            reviewer = st.text_input("别名审核人")
            relation = st.text_area("简称与主体的关系依据", height=90)
            confirmed = st.checkbox("已核实该别名在以上地区和行业内对应此主体")
            submit = st.form_submit_button("单独保存企业别名")
        if submit:
            if not confirmed:
                st.error("请确认别名与主体的对应关系及适用范围。")
            else:
                try:
                    repo.save_alias(
                        alias=alias,
                        company_name=company,
                        area=area,
                        industry=industry,
                        source=source,
                        reviewer=reviewer,
                        relation=relation,
                    )
                except ValueError as exc:
                    st.error(str(exc))
                except sqlite3.Error:
                    st.error("别名保存失败，请稍后重试。")
                else:
                    st.success("企业别名已保存；只对后续分析生效。")
        aliases = repo.aliases()
        if aliases:
            st.dataframe(
                [
                    {
                        "别名": a["alias"],
                        "主体": a["company_name"],
                        "地区": a["area"],
                        "行业": a["industry"],
                        "来源": a["source"],
                        "审核人": a["reviewer"],
                        "关系依据": a["relation"],
                    }
                    for a in aliases
                ],
                hide_index=True,
                width="stretch",
            )


def render_evidence(analysis):
    extraction = analysis.get("extraction") or {}
    company = analysis.get("company") or {}
    intent = analysis.get("intent") or {}
    st.subheader("企业核实", anchor=False)
    st.text(company.get("reason") or "暂无企业核实依据")
    quotes(extraction.get("company_evidence"))
    for index, candidate in enumerate(company.get("candidates") or [], start=1):
        with st.container(border=True):
            st.text(f"候选 {index}：{candidate.get('name', '未知')}")
            st.caption(
                f"匹配分 {candidate.get('score', 0):.0%} · 地区匹配 {'是' if candidate.get('area_match') else '否'} · 行业匹配 {'是' if candidate.get('industry_match') else '否'} · 主体关系有依据 {'是' if candidate.get('relation_supported') else '否'}"
            )
            st.text(candidate.get("quote") or "暂无来源摘录")
            if candidate.get("relation_quote"):
                with st.expander("查看主体关系依据"):
                    st.text(candidate["relation_quote"])
                    source_link(
                        "查看主体关系来源",
                        candidate.get("relation_source_url") or candidate.get("source_url"),
                    )
            source_link("查看候选来源", candidate.get("source_url"))
            if candidate.get("scope_evidence"):
                with st.expander("查看范围核实依据"):
                    for evidence in candidate["scope_evidence"]:
                        st.text(evidence["content"])
                        source_link("打开范围依据来源", evidence["url"])
    for evidence in company.get("evidence") or []:
        with st.expander(evidence.get("title") or "企业查询来源"):
            st.text(evidence.get("content", ""))
            source_link("打开来源网页", evidence.get("url"))
            st.caption("查询时间：" + (evidence.get("retrieved_at") or "未记录"))
    st.subheader("意向信号", anchor=False)
    st.markdown(
        intent_reason_table(
            {"intent_reason": intent.get("reason"), "intent_level": intent.get("level")},
            analysis,
            detailed=True,
        ),
        unsafe_allow_html=True,
    )
    st.caption("信号值为 0、0.5 或 1；未知表示该维度暂无明确依据，原始值保留为 null。")
    lower, upper = intent.get("score_lower"), intent.get("score_upper")
    if lower is not None and upper is not None:
        if lower == upper:
            st.caption(f"意向评分：{lower:g} / 100")
        else:
            st.caption(f"意向评分区间：{lower:g} – {upper:g} / 100；未知维度的贡献暂未确定。")
    st.subheader("事实与摘要依据", anchor=False)
    for field, fact in (extraction.get("facts") or {}).items():
        with st.expander(FIELDS.get(field, field) + " · " + shown(fact.get("value"))):
            if fact.get("approximate"):
                st.caption("原文为近似描述。")
            quotes(fact.get("evidence"))
    if extraction.get("summary_evidence"):
        st.markdown("##### 需求摘要的原文依据")
        quotes(extraction["summary_evidence"])
    if extraction.get("refusal_evidence"):
        st.caption("明确拒绝依据")
        quotes(extraction["refusal_evidence"])
    for warning in extraction.get("warnings") or []:
        st.warning(warning)


def render_history(repo, run):
    history = repo.history(run["run_id"])
    if not history:
        st.info("尚无人工修改记录。")
    for event in history:
        with st.expander(
            f"v{event['from_version']} → v{event['to_version']} · {event['reviewer']} · {timestamp_label(event['created_at'])}"
        ):
            st.text("操作：" + ("审核通过" if event["action"] == "approve" else "人工修正"))
            st.text("说明：" + event["reason"])
            rows = diff_rows(event["before"], event["after"])
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            with st.expander("完整前后值"):
                st.json({"before": event["before"], "after": event["after"]})


def render_run(repo, run):
    st.divider()
    st.subheader("线索详情", anchor=False)
    analysis, current = run["analysis"], run["current"]
    st.caption(
        f"{STATUSES.get(run['run_status'], run['run_status'])} · {REVIEWS[run['review_status']]} · 版本 {run['version']}"
    )
    if current is None or run["run_status"] == "failed":
        st.error(
            "分析失败。"
            + ERRORS.get(analysis.get("error_code"), "本次分析未生成可用结果，请稍后重试。")
        )
        with st.expander("查看本次通话文本"):
            st.text(run["transcript"])
        return
    original = run["original"] or {}
    score = original.get("confidence_score")
    score_label = f"{score:.0%}" if score is not None else "未知"
    st.markdown(
        '<div class="lead-overview"><div class="lead-stat"><span>企业主体</span>'
        f"<strong>{escape(current['company_name'] or '待核实企业')}</strong>"
        f"<small>{escape(REVIEWS[run['review_status']])} · v{run['version']}</small></div>"
        '<div class="lead-stat"><span>意向等级</span>'
        f"<strong>{INTENTS[current['intent_level']]}</strong><small>当前线索意向</small></div>"
        '<div class="lead-stat"><span>置信度</span>'
        f"<strong>{score_label}</strong><small>原始评估 · 非购买概率</small></div></div>",
        unsafe_allow_html=True,
    )
    if run["review_status"] == "approved":
        st.success("人工审核已完成；原始机器评估和证据保留在记录中。")
    elif original.get("need_human_review", True):
        st.warning("机器建议人工复核。企业主体、意向和关键事实需要结合证据检查。")
    else:
        st.info("机器未触发强制复核；此记录仍未经过人工审核。")
    reasons = (
        analysis.get("review_reasons")
        or (analysis.get("quality") or {}).get("review_reasons")
        or []
    )
    if reasons:
        with st.expander("查看机器复核原因", expanded=True):
            for reason in reasons:
                st.text("• " + reason)
    summary, evidence, review, history = st.tabs(
        ["结果与原文", "证据与来源", "人工修正", "审核轨迹"]
    )
    with summary:
        render_lead_summary(current, analysis)
        with st.expander("通话原文"):
            st.text(run["transcript"])
        st.download_button(
            "下载本条 JSON",
            json_text(repo.export([run["run_id"]])[0]),
            file_name=f"lead-{run['run_id']}.json",
            mime="application/json",
            key="download_" + run["run_id"],
        )
        with st.expander("查看 15 字段 JSON"):
            st.json(current)
        with st.expander("查看原始机器结果与版本"):
            st.caption(f"记录 {run['run_id']} · 创建于 {timestamp_label(run['created_at'])}")
            st.caption(
                f"耗时 {analysis.get('elapsed_ms', 0) / 1000:.1f} 秒 · 模型请求 {analysis.get('request_count', 0)} 次"
            )
            st.json(
                {
                    "original": original,
                    "quality": analysis.get("quality"),
                    "versions": analysis.get("versions"),
                }
            )
    with evidence:
        render_evidence(analysis)
    with review:
        edit_form(repo, run)
        alias_form(repo, run)
    with history:
        render_history(repo, run)


def workbench(settings, repo):
    page_heading(
        "把通话，变成下一步。",
        "从客户原话中提取事实、核实企业、判断意向，让每条线索都有据可查。",
    )
    with st.container(key="workbench_layout"):
        input_col, guide_col = st.columns([2.1, 1], gap="large")
        with input_col, st.container(key="input_card", border=False):
            st.subheader("通话文本", anchor=False)
            st.caption("保留说话人标记，帮助区分客户事实与销售介绍。")
            with st.form("analyze", border=False):
                text = st.text_area(
                    "粘贴通话文本",
                    key="text",
                    height=300,
                    max_chars=20000,
                    placeholder="销售：您好，想了解一下您目前的业务需求。\n\n客户：我们目前有……",
                    help="建议保留客户与销售的说话人标记，最多 20,000 字。",
                )
                call_time = call_time_picker("通话日期（可选）", key="analysis_call_time")
                _, action_col = st.columns([1.6, 1])
                with action_col:
                    submitted = st.form_submit_button(
                        "开始分析", type="primary", disabled=not settings.api_key, width="stretch"
                    )
            st.caption("提交后由 DeepSeek 分析文本，结果和审核记录保存在本机。")
        with guide_col:
            st.markdown(
                '<aside class="guide-card" aria-label="分析流程说明">'
                '<h3>每一个判断，都留有依据。</h3><ol class="guide-steps" role="list">'
                '<li class="guide-step"><span class="step-number" aria-hidden="true">1</span><div><strong>提取客户事实</strong>'
                "<p>整理联系人、业务规模与需求，区分客户表述和销售介绍。</p></div></li>"
                '<li class="guide-step"><span class="step-number" aria-hidden="true">2</span><div><strong>核实企业与意向</strong>'
                "<p>结合企业来源和原文信号，展示判断依据与不确定项。</p></div></li>"
                '<li class="guide-step"><span class="step-number" aria-hidden="true">3</span><div><strong>由你确认结果</strong>'
                "<p>对照证据修正，预览修改差异，审核后导出结构化线索。</p></div></li></ol>"
                '<div class="guide-note"><strong>未知，也是一种准确的结果。</strong><br>'
                "无法确认的信息统一保留为 null，留待人工核实。</div></aside>",
                unsafe_allow_html=True,
            )
    if submitted:
        if not text.strip():
            st.error("请先填写通话文本。")
        else:
            try:
                request = AnalysisInput(transcript=text, call_time=call_time)
            except ValidationError:
                st.error("请检查通话文本长度及所选通话时间。")
            else:
                with st.status("正在分析通话…", expanded=True) as status:
                    try:
                        result = Workflow(settings, repo, progress=st.write).analyze(request)
                    except (ValueError, sqlite3.Error, OSError):
                        status.update(label="本次分析未完成", state="error")
                        st.error("分析或本地保存未完成，请检查服务配置和本地存储后重试。")
                    else:
                        st.session_state["last_run_id"] = result.run_id
                        status.update(
                            label="分析已结束，结果已保存",
                            state="error" if result.run_status == "failed" else "complete",
                            expanded=False,
                        )
    if st.session_state.get("last_run_id"):
        try:
            render_run(repo, repo.get_run(st.session_state["last_run_id"]))
        except KeyError:
            st.info("上次结果已不在当前数据库中，请前往线索与审核查看现有记录。")
    else:
        st.markdown(
            '<div class="empty-note">分析结果将在这里展开，历史记录可在「线索与审核」中查看。</div>',
            unsafe_allow_html=True,
        )


def run_label(run):
    return f"{(run['current'] or {}).get('company_name') or '待核实企业'} · {timestamp_label(run['created_at'])[:16]} · {run['run_id'][:8]}"


def batch_form(repo, runs):
    eligible = {
        run["run_id"]: run
        for run in runs
        if run["current"] is not None and run["run_status"] in ("succeeded", "partial")
    }
    if not eligible:
        return
    with st.expander("批量修正与导出"):
        selected = st.multiselect(
            "选择待处理记录",
            list(eligible),
            format_func=lambda value: run_label(eligible[value]),
            key="batch_ids",
        )
        st.caption("所选记录由稳定的记录编号关联；筛选变化不会按行号误选。")
        if selected:
            st.download_button(
                "导出所选 JSON",
                json_text(repo.export(selected)),
                file_name="leads.json",
                mime="application/json",
            )
        with st.form("batch_review"):
            field = st.selectbox(
                "批量修改字段",
                ["company_name", "intent_level", "review_only"],
                format_func=lambda value: (
                    "仅审核，保留各自字段" if value == "review_only" else FIELDS[value]
                ),
            )
            company = st.text_input("统一企业主体", help="选择企业主体时使用；留空即清空为 null。")
            intent = st.selectbox("统一意向等级", list(INTENTS), format_func=INTENTS.get)
            reviewer = st.text_input("批量审核人")
            reason = st.text_area("批量修正 / 核实说明", height=90)
            approve = st.checkbox("将所选记录同时标记为已审核")
            checked = st.checkbox("已逐条核查所选记录的原文和证据")
            accept_unknown = st.checkbox("已检查并接受所选记录中的未知企业 / 意向")
            submit = st.form_submit_button("预览批量修改")
        if submit:
            st.session_state.pop("batch_preview", None)
            if not selected:
                st.error("请先选择待处理记录。")
            else:
                value = (company.strip() or None) if field == "company_name" else intent
                changes = [
                    {
                        "run_id": run_id,
                        "expected_version": eligible[run_id]["version"],
                        "updates": {} if field == "review_only" else {field: value},
                        "approve": approve,
                        "accept_unknown": accept_unknown,
                    }
                    for run_id in selected
                ]
                capture_preview(
                    "batch_preview",
                    changes,
                    {run_id: eligible[run_id] for run_id in selected},
                    reviewer,
                    reason,
                    checked,
                )
        render_preview(repo, "batch_preview", "确认保存整批修改")


def history_page(repo):
    page_heading("线索与审核", "集中查看分析结果，对照证据确认信息，保留每一次修正。")
    st.caption("创建与审核时间均按北京时间（UTC+8）显示。")
    runs = repo.list_runs()
    with st.container(key="history_stats"):
        cols = st.columns(3)
        cols[0].metric("全部分析", len(runs))
        cols[1].metric(
            "未人工审核",
            sum(run["review_status"] != "approved" and run["current"] is not None for run in runs),
        )
        cols[2].metric("已审核", sum(run["review_status"] == "approved" for run in runs))
    if not runs:
        st.info("还没有分析记录。前往分析工作台，粘贴一段通话文本即可开始。")
        return
    with st.container(key="history_filters"):
        filters = st.columns(3)
        intent_filter = filters[0].selectbox(
            "筛选意向", ["全部", "未知", "高意向", "中意向", "低意向"]
        )
        review_filter = filters[1].selectbox("筛选审核状态", ["全部", *REVIEWS.values()])
        date_filter = filters[2].selectbox("创建时间", ["全部时间", "最近 7 天", "最近 30 天"])
    cutoff = datetime.now(timezone.utc) - timedelta(days=7 if date_filter == "最近 7 天" else 30)
    filtered = [
        run
        for run in runs
        if (
            intent_filter == "全部"
            or INTENTS[(run["current"] or {}).get("intent_level")] == intent_filter
        )
        and (review_filter == "全部" or REVIEWS[run["review_status"]] == review_filter)
        and (date_filter == "全部时间" or datetime.fromisoformat(run["created_at"]) >= cutoff)
    ]
    if not filtered:
        st.info("当前筛选没有记录，请调整筛选条件。")
        return
    st.dataframe(
        [
            {
                "企业主体": (run["current"] or {}).get("company_name") or "待核实",
                "联系人": (run["current"] or {}).get("contact_name") or "—",
                "意向": INTENTS[(run["current"] or {}).get("intent_level")],
                "分析状态": STATUSES.get(run["run_status"], run["run_status"]),
                "审核状态": REVIEWS[run["review_status"]],
                "创建时间": timestamp_label(run["created_at"]),
                "记录编号": run["run_id"],
            }
            for run in filtered
        ],
        hide_index=True,
        width="stretch",
    )
    batch_form(repo, filtered)
    lookup = {run["run_id"]: run for run in filtered}
    if st.session_state.get("selected_run") not in lookup:
        st.session_state["selected_run"] = next(iter(lookup))
    selected = st.selectbox(
        "查看并审核一条线索",
        list(lookup),
        format_func=lambda value: run_label(lookup[value]),
        key="selected_run",
    )
    render_run(repo, lookup[selected])


def main():
    st.set_page_config(page_title="AI SDR Agent · 通话线索工作台", page_icon="✳", layout="wide")
    st.markdown(
        "<style>" + Path(__file__).with_name("styles.css").read_text(encoding="utf-8") + "</style>",
        unsafe_allow_html=True,
    )
    settings = Settings.from_env()
    try:
        repo = Repository(settings.database_path)
    except (sqlite3.Error, OSError):
        st.error("无法打开本地数据文件，请检查 DATABASE_PATH 和目录写入权限。")
        return
    with st.sidebar:
        st.markdown(
            '<div class="brand"><svg class="brand-mark" width="30" height="30" viewBox="0 0 30 30" '
            'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
            'aria-hidden="true"><path d="M6 5h18a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H12l-6 4v-4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z"/>'
            '<path d="M10 11h10M10 16h6"/></svg><div><strong>AI SDR</strong>'
            "<small>通话线索工作台</small></div></div>",
            unsafe_allow_html=True,
        )
        page = st.radio("工作区", ["分析工作台", "线索与审核"], label_visibility="collapsed")
        st.divider()
        st.markdown(
            '<div class="sidebar-status"><strong><span class="status-dot"></span>'
            + ("模型密钥已配置" if settings.api_key else "等待配置模型")
            + "</strong><br>"
            + escape(settings.model)
            + "<br>企业查询 · "
            + ("已开启" if settings.search_enabled else "已关闭")
            + '</div><div class="sidebar-note">文本分析 · 企业核实 · 人工审核<br>分析与审核记录保存在本机</div>',
            unsafe_allow_html=True,
        )
    if not settings.api_key:
        st.warning(
            "尚未配置 DEEPSEEK_API_KEY。请在本地 .env 中填写后重新启动；历史记录和人工审核仍可使用。"
        )
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    if page == "分析工作台":
        workbench(settings, repo)
    else:
        history_page(repo)
