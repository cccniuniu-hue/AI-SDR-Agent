"""Validate model suggestions against numbered turns before downstream decisions."""

import re

from .schemas import FACT_FIELDS, AnalysisInput, Evidence, Extraction, Fact, Turn, aware_time

_SPEAKERS = re.compile(
    r"(?:^|(?<=\n)|(?<=[。！？!?]))[ \t]*(客户|销售|顾客|客服|customer|sales)[ \t]*[:：]",
    re.IGNORECASE,
)
_APPROXIMATE = re.compile(
    r"大约|大概|左右|上下|差不多|约\s*\d|\d+\s*[多余几+＋]|[十百千万]来|几十|几百|若干|至少|以上|以下|不到|将近|接近|大致|二三|三四|四五|五六|六七|七八|八九|[0-9]+\s*[-~～至到]\s*[0-9]+"
)
_AGREEMENT = re.compile(
    r"^(?:对[的啊，,。！!\s]*|是的[，,。！!\s]*|没错[，,。！!\s]*|对的[，,。！!\s]*|是我[，,。！!\s]*|嗯[，,。！!\s]*是我)(?:是我|我就是|您说|你说|你讲|请讲|什么事|有什么事|[。！!，,\s])*"
)
_CHINESE_DIGITS = dict(zip("零一二三四五六七八九两", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
_CHINESE_APPROXIMATE = re.compile(
    r"两三|[零一二三四五六七八九两十百千万]+[多余几来]|[两二三四五六七八九]十[几多]"
)
_HEADER = re.compile(
    r"(?:通话记录|通话开始|电话记录|对话记录|通话文本|转写文本|录音转写)[：:。\s]*"
)


def split_turns(text: str) -> list[Turn]:
    """Use 1-based IDs. Continuations retain the last explicit speaker."""
    matches = list(_SPEAKERS.finditer(text))
    turns = []
    if not matches:
        return [Turn(turn_id=1, speaker="unknown", text=text.strip())] if text.strip() else []
    prefix = text[: matches[0].start()].strip()
    if prefix:
        turns.append(Turn(turn_id=1, speaker="unknown", text=prefix))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end() : end].strip()
        label = match.group(1).lower()
        speaker = "customer" if label in {"客户", "顾客", "customer"} else "sales"
        if content:
            turns.append(Turn(turn_id=len(turns) + 1, speaker=speaker, text=content))
    return turns


def _evidence_valid(evidence: Evidence, turns: dict[int, Turn], customer_only=True) -> bool:
    turn = turns.get(evidence.turn_id)
    return bool(
        turn
        and evidence.quote.strip()
        and evidence.speaker == turn.speaker
        and evidence.quote in turn.text
        and (not customer_only or turn.speaker == "customer")
    )


def _all_evidence(evidence: list[Evidence], turns: dict[int, Turn]) -> bool:
    return bool(evidence) and all(_evidence_valid(item, turns) for item in evidence)


def _chinese_number(token: str) -> int:
    total = current = 0
    for char in token:
        if char in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[char]
        else:
            unit = {"十": 10, "百": 100, "千": 1000, "万": 10000}[char]
            if unit == 10000:
                total = (total + current) * unit
            else:
                total += (current or 1) * unit
            current = 0
    return total + current


def _count_supported(value: int, quote: str, field: str) -> bool:
    number = r"(\d+|[零一二三四五六七八九两十百千万]+)"
    noun = r"(?:门店|店铺|分店|店)" if field == "mendian" else r"(?:设计师|设计人员|设计团队)"
    unit = r"(?:家|间)" if field == "mendian" else r"(?:人|位|名|个)"
    patterns = [
        number + rf"\s*{unit}?\s*{noun}",
        noun + r"[^\d零一二三四五六七八九两十百千万，。；\n]{0,12}" + number,
    ]
    tokens = [token for pattern in patterns for token in re.findall(pattern, quote)]
    for token in tokens:
        if (int(token) if token.isdigit() else _chinese_number(token)) == value:
            return True
    return False


def _confirmed_contact(fact: Fact, turns: dict[int, Turn]) -> bool:
    if not fact.evidence or not all(
        _evidence_valid(item, turns, customer_only=False) for item in fact.evidence
    ):
        return False
    for evidence in fact.evidence:
        turn = turns[evidence.turn_id]
        following = turns.get(turn.turn_id + 1)
        if (
            turn.speaker == "sales"
            and str(fact.value) in evidence.quote
            and following
            and following.speaker == "customer"
        ):
            agreement = any(
                item.turn_id == following.turn_id and _evidence_valid(item, turns)
                for item in fact.evidence
            )
            if agreement and _AGREEMENT.fullmatch(following.text.strip()):
                return True
    return False


def validate_extraction(extraction: Extraction, input: AnalysisInput) -> Extraction:
    """Return a copy with unsupported facts set to null, retaining quotes for audit."""
    result = extraction.model_copy(deep=True)
    result.model_notes = list(dict.fromkeys(result.model_notes + result.warnings))
    result.warnings = []
    turns = {turn.turn_id: turn for turn in split_turns(input.transcript)}
    result.role_uncertain = result.role_uncertain or any(
        turn.speaker == "unknown" and not _HEADER.fullmatch(turn.text) for turn in turns.values()
    )
    if input.call_time:
        result.facts["call_time"] = Fact(value=input.call_time)
    attempted = sum(
        result.facts.get(key) is not None and result.facts[key].value is not None
        for key in FACT_FIELDS
    )
    accepted = 0
    for key, fact in result.facts.items():
        if fact.value is None:
            continue
        quotes = "\n".join(item.quote for item in fact.evidence)
        valid = key in FACT_FIELDS and _all_evidence(fact.evidence, turns)
        if key == "contact_name" and not valid:
            valid = _confirmed_contact(fact, turns)
        if key in {"mendian", "shejishi"}:
            valid = (
                valid
                and isinstance(fact.value, int)
                and not isinstance(fact.value, bool)
                and not fact.approximate
                and not _APPROXIMATE.search(quotes)
                and not _CHINESE_APPROXIMATE.search(quotes)
                and _count_supported(fact.value, quotes, key)
            )
        elif key == "phone":
            valid = (
                valid
                and isinstance(fact.value, str)
                and re.sub(r"[\s()+-]", "", fact.value) in re.sub(r"[\s()+-]", "", quotes)
            )
        elif key == "call_time":
            try:
                valid = (
                    valid
                    and isinstance(fact.value, str)
                    and aware_time(fact.value) is not None
                    and str(fact.value) in quotes
                )
            except ValueError:
                valid = False
            if input.call_time:
                valid = True
                fact.value = input.call_time
        else:
            valid = (
                valid
                and isinstance(fact.value, str)
                and bool(fact.value.strip())
                and fact.value in quotes
            )
        # Check the full referenced turn so a model cannot hide negation by quoting only a value.
        contexts = "\n".join(
            turns[item.turn_id].text for item in fact.evidence if item.turn_id in turns
        )
        if key in {"mendian", "shejishi"} and (
            _APPROXIMATE.search(contexts) or _CHINESE_APPROXIMATE.search(contexts)
        ):
            valid = False
        if re.search(
            r"(?:不是|不在|不叫|并非|没有|不属于)[^，。；\n]{0,8}" + re.escape(str(fact.value)),
            contexts,
        ):
            valid = False
        if key == "contact_title":
            valid = valid and bool(
                re.search(
                    r"(?:我是|我担任|我的职务是|我的职位是|职位为|职务为|我负责)[^，。；\n]{0,8}"
                    + re.escape(str(fact.value)),
                    contexts,
                )
            )
        if valid:
            accepted += 1
        else:
            fact.value = None
            result.warnings.append(f"字段 {key} 缺少可核对的客户直接证据，已置空")
    if input.call_time:
        result.facts["call_time"] = Fact(value=input.call_time)
    if result.company_mention and (
        not _all_evidence(result.company_evidence, turns)
        or not any(result.company_mention in item.quote for item in result.company_evidence)
    ):
        result.company_mention = None
        result.warnings.append("企业提及缺少客户直接证据，已置空")
    for key, signal in result.signals.items():
        if signal.value is not None and not _all_evidence(signal.evidence, turns):
            signal.value = None
            result.warnings.append(f"意向信号 {key} 缺少客户直接证据，已置空")
    refusal_quotes = "\n".join(item.quote for item in result.refusal_evidence)
    refusal_words = re.search(
        r"不需要|没需求|没有需求|不考虑|不采购|不购买|不买|拒绝|别再|不要再|不感兴趣|不用了|别联系|不要联系|不再联系",
        refusal_quotes,
    )
    if result.refusal and (not _all_evidence(result.refusal_evidence, turns) or not refusal_words):
        result.refusal = False
        result.warnings.append("拒绝标记缺少客户直接证据，已取消")
    if result.refusal:
        last_refusal_turn = max(item.turn_id for item in result.refusal_evidence)
        refusal_offsets = list(
            re.finditer(
                r"不需要|没需求|没有需求|不考虑|不采购|不购买|不买|拒绝|别再|不要再|不感兴趣|不用了|别联系|不要联系|不再联系",
                turns[last_refusal_turn].text,
            )
        )
        refusal_end = (last_refusal_turn, refusal_offsets[-1].end() if refusal_offsets else 0)
        later = "\n".join(
            turn.text[refusal_end[1] :] if turn.turn_id == refusal_end[0] else turn.text
            for turn in turns.values()
            if turn.speaker == "customer" and turn.turn_id >= refusal_end[0]
        )
        if re.search(
            r"(?:但|不过|现在|目前|改为).{0,20}(?:准备采购|需要|要买|采购|安排演示|想试用)", later
        ):
            result.refusal = False
            result.conflicted = True
            result.warnings.append("拒绝证据后出现立场变化，取消拒绝判定并交人工复核")
    if result.demand_summary and not _all_evidence(result.summary_evidence, turns):
        result.demand_summary = None
        result.warnings.append("需求摘要缺少客户直接证据，已置空")
    result.fact_evidence_ratio = accepted / attempted if attempted else None
    result.warnings = list(dict.fromkeys(result.warnings))
    return result
