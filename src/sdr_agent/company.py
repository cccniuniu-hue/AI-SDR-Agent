"""Conservative company disambiguation using only retrieved body text."""

import re
import time
from urllib.parse import urlsplit

from .deepseek import DeepSeekError
from .schemas import CompanyCandidate, CompanyResolution, Extraction, SearchResult

_ENTITY = re.compile(
    r"(?:公司|企业|工作室|经营部|商行|事务所|合作社|有限公司|有限责任公司|股份有限公司)$"
)
_NEGATED_RELATION = re.compile(r"不是|并非|并不|不属于|不等于|无关|否认|非其|非本")
_BRAND_ONLY = re.compile(r"品牌总部|品牌所有|加盟|经销商|代理商|授权店|特许经营|集团成员|股东")
_OPERATING_ENTITY = re.compile(
    r"(?:经营主体|运营主体|注册主体|营业执照|运营公司)|(?:门店|店铺|分店).{0,30}由.{0,40}(?:经营|运营)"
)


def _fact(extraction: Extraction, key: str) -> str | None:
    fact = extraction.facts.get(key)
    return str(fact.value).strip() if fact and fact.value is not None else None


def _public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def company_mentions(extraction: Extraction) -> list[str]:
    """Use only explicit customer self-aliases, never spelling similarity as proof."""
    mention = extraction.company_mention
    if not mention:
        return []
    escaped = re.escape(mention)
    for evidence in extraction.company_evidence:
        if evidence.speaker == "customer" and re.search(
            r"(?:我们|本公司|我司)(?:不是|并非|不属于)[\s“\"‘]*"
            + escaped
            + "|"
            + escaped
            + r"[”\"’\s]*(?:是|为)(?:我们|我司|本公司)?的?(?:供应商|客户|竞品|合作方)",
            evidence.quote,
        ):
            return []
    names = [mention]
    area, industry = _fact(extraction, "area"), _fact(extraction, "industry")
    allowed_prefixes = {""}
    if area:
        allowed_prefixes.add(f"在{area}")
    if industry:
        allowed_prefixes.add(f"做{industry}")
    if area and industry:
        allowed_prefixes.add(f"在{area}，做{industry}")
    for evidence in extraction.company_evidence:
        if evidence.speaker != "customer":
            continue
        for sentence in re.split(r"[。！？!?\n]", evidence.quote):
            if mention not in sentence or _NEGATED_RELATION.search(sentence):
                continue
            tail = sentence.split(mention, 1)[1].strip(' \t，,（(”"’')
            match = re.search(
                r"(?:也叫|又叫|也就是(?:大家说的)?|简称(?:为|是)?)[:：\s“\"‘]*"
                r"([\u4e00-\u9fffA-Za-z0-9·]{2,60})(?=[”\"’）)，,；;\s]|$)",
                tail,
            )
            if match and tail[: match.start()].strip(" ，,") in allowed_prefixes:
                alias = match[1]
                if not alias.isdigit() and alias not in names:
                    names.append(alias)
    return names[:3]


def _reviewed_alias(extraction: Extraction, aliases: list[dict]) -> CompanyResolution | None:
    context = " ".join(item.quote for item in extraction.company_evidence)
    if _BRAND_ONLY.search(context) or re.search(r"独立经营|独立法人|分公司|分店", context):
        return None
    area, industry = _fact(extraction, "area"), _fact(extraction, "industry")
    matches = []
    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        # An unknown customer scope cannot establish equality with a reviewed scope.
        if (
            not area
            or not industry
            or alias.get("alias") not in company_mentions(extraction)
            or alias.get("area") != area
            or alias.get("industry") != industry
        ):
            continue
        if not all(
            isinstance(alias.get(key), str) and alias[key].strip()
            for key in ("company_name", "source", "reviewer", "relation")
        ):
            continue
        matches.append(alias)
    if not matches:
        return None
    names = {alias["company_name"] for alias in matches}
    if len(names) != 1:
        return CompanyResolution(
            status="ambiguous", reason="同一适用范围存在不同企业的人工别名记录，需复核"
        )
    alias = matches[0]
    source = SearchResult(
        url=alias["source"],
        title="已审核的范围限定别名",
        content=f"{alias['alias']}：{alias['company_name']}。{alias['relation']}。审核人：{alias['reviewer']}",
    )
    candidate = CompanyCandidate(
        name=alias["company_name"],
        source_url=source.url,
        quote=source.content,
        relation_quote=alias["relation"],
        area_match=True,
        industry_match=True,
        relation_supported=True,
        score=1,
    )
    return CompanyResolution(
        selected_name=candidate.name,
        candidates=[candidate],
        status="confirmed",
        evidence=[source],
        quality_score=1,
        reason="精确命中已审核别名，客户地区和行业均与适用范围相同",
    )


def _direct_relation(mention: str, name: str, quote: str) -> bool:
    if not mention or _NEGATED_RELATION.search(quote):
        return False
    if mention == name:
        return name in quote
    alias, full = re.escape(mention), re.escape(name)
    punctuation = r'[\s“”"\u2018\u2019\(\)（）:：，,]*'
    patterns = [
        alias + punctuation + r"(?:是|为)" + punctuation + full + r"的(?:简称|全称)",
        alias
        + punctuation
        + r"(?:的)?(?:公司全称|全称|经营主体|运营主体|注册主体|公司名称|工商登记名称|注册名称)"
        + punctuation
        + r"(?:是|为)?"
        + punctuation
        + full,
        full
        + punctuation
        + r"(?:以下)?(?:简称|下称)(?:为|是)?"
        + punctuation
        + alias
        + r"(?![\u4e00-\u9fff])",
        alias + r"(?:门店|店铺)?由" + punctuation + full + r"(?:经营|运营)",
    ]
    return any(re.search(pattern, quote) for pattern in patterns)


def _entity_scope(name: str, sources: list[SearchResult]) -> list[SearchResult]:
    evidence = []
    for source in sources:
        for match in re.finditer(re.escape(name), source.content):
            snippet = source.content[match.start() : match.start() + 500].strip()
            other_text = snippet.replace(name, "")
            if re.search(
                r"有限公司|有限责任公司|股份公司|工作室|经营部|商行|事务所|合作社", other_text
            ):
                continue
            if re.search(r"分公司|子公司|办事处|工作地点|招聘", snippet):
                continue
            evidence.append(source.model_copy(update={"content": snippet}))
            break
        if len(evidence) == 5:
            break
    return evidence


def _score(
    candidate: CompanyCandidate,
    source: SearchResult,
    extraction: Extraction,
    sources: list[SearchResult] | None = None,
) -> tuple[CompanyCandidate, bool, bool]:
    mention = extraction.company_mention or ""
    body = source.content
    quote_valid = bool(
        candidate.quote
        and candidate.quote in body
        and candidate.name in candidate.quote
        and _ENTITY.search(candidate.name)
    )
    relation_quote = candidate.relation_quote
    if not relation_quote and quote_valid and candidate.name in company_mentions(extraction):
        relation_quote = candidate.quote
    relation_source = source.url
    scope_evidence = _entity_scope(candidate.name, sources or [source]) if quote_valid else []
    relation_valid = bool(
        relation_quote
        and relation_quote in body
        and any(
            _direct_relation(name, candidate.name, relation_quote)
            for name in company_mentions(extraction)
        )
    )
    if not relation_valid and quote_valid and (not relation_quote or relation_quote in body):
        for evidence in scope_evidence:
            if any(
                _direct_relation(name, candidate.name, evidence.content)
                for name in company_mentions(extraction)
            ):
                relation_quote, relation_source, relation_valid = (
                    evidence.content,
                    evidence.url,
                    True,
                )
                break
    brand_ambiguity = bool(
        (_BRAND_ONLY.search(mention) or _BRAND_ONLY.search(relation_quote))
        and not _OPERATING_ENTITY.search(relation_quote)
    )
    if brand_ambiguity:
        relation_valid = False
    customer_context = " ".join(item.quote for item in extraction.company_evidence)
    if _BRAND_ONLY.search(customer_context) or re.search(
        r"独立经营|独立法人|分公司|分店", customer_context
    ):
        brand_ambiguity = True
        relation_valid = False
    area, industry = _fact(extraction, "area"), _fact(extraction, "industry")
    # Require matches near the actual cited entity, never elsewhere in a directory page.
    cited = (
        candidate.quote + "\n" + (relation_quote if relation_quote in body else "")
        if quote_valid
        else ""
    )
    location_pattern = r"(?:位于|总部\s*[:：]?|地址\s*[:：]?|注册地\s*[:：]?|经营地\s*[:：]?|所在地\s*[:：]?)\s*([^，。；\n]+)"
    scope_text = "\n".join(item.content for item in scope_evidence)
    area_match = bool(
        area
        and (
            area in cited
            or any(area in value for value in re.findall(location_pattern, scope_text))
        )
    )
    if quote_valid:
        cited += "\n" + scope_text
    industry_match = bool(industry and industry in cited)
    industry_conflict = False
    if industry:
        negative = re.search(
            r"(?:不从事|不经营|不是|并非)[^，。；\n]{0,8}" + re.escape(industry), cited
        )
        businesses = re.findall(r"(?:主营|行业为|专注于?|从事)([^，。；\n]+)", candidate.quote)
        tokens = {industry[i : i + 2] for i in range(len(industry) - 1)}
        industry_conflict = bool(negative) or bool(
            not industry_match
            and businesses
            and not any(token in business for token in tokens for business in businesses)
        )
        if industry_conflict:
            industry_match = False
    locations = re.findall(location_pattern, cited)
    if area and locations and any(area not in location for location in locations):
        area_match = False
    if area and re.search(r"(?:不在|不位于|并非位于|不设在)\s*" + re.escape(area), cited):
        area_match = False
    score = round(
        0.30 * quote_valid + 0.35 * relation_valid + 0.20 * area_match + 0.15 * industry_match, 2
    )
    verified = candidate.model_copy(
        update={
            "score": score,
            "area_match": area_match,
            "industry_match": industry_match,
            "relation_supported": relation_valid,
            "quote": candidate.quote if quote_valid else "",
            "relation_quote": relation_quote if relation_valid else "",
            "relation_source_url": relation_source if relation_valid else None,
            "scope_evidence": scope_evidence,
        }
    )
    # Unmatched industry wording earns no points, but does not itself prove a contradiction.
    eligible = (
        quote_valid
        and relation_valid
        and (area is None or area_match)
        and not industry_conflict
        and not brand_ambiguity
    )
    return verified, eligible, brand_ambiguity


def resolve_company(
    client,
    extraction: Extraction,
    deadline: float,
    aliases: list[dict] | None = None,
    progress=None,
) -> CompanyResolution:
    progress = progress or (lambda message: None)
    mention = extraction.company_mention
    if not mention:
        return CompanyResolution(status="skipped", reason="客户未提供可核对的企业称呼")
    names = company_mentions(extraction)
    if not names:
        return CompanyResolution(reason="客户明确否认该称呼为自身主体，或该名称仅指其他业务方")
    reviewed = _reviewed_alias(extraction, aliases or [])
    if reviewed:
        return reviewed
    area, industry = _fact(extraction, "area"), _fact(extraction, "industry")
    queries = list(
        dict.fromkeys(
            " ".join(part for part in parts if part)[:128]
            for parts in [(name, area, industry) for name in names] + [(mention, "公司全称", area)]
        )
    )[:2]
    sources: dict[str, SearchResult] = {}
    unavailable = False
    for index, query in enumerate(queries, start=1):
        if time.monotonic() >= deadline:
            unavailable = True
            break
        try:
            progress(f"检索企业来源（{index}/{len(queries)}）")
            for source in client.search(query, deadline)[:5]:
                if _public_url(source.url) and (
                    source.url not in sources
                    or len(source.content) > len(sources[source.url].content)
                ):
                    sources[source.url] = source
        except DeepSeekError:
            unavailable = True
            break
    if not sources:
        return CompanyResolution(
            status="unavailable" if unavailable else "unresolved",
            reason="企业检索服务暂不可用" if unavailable else "没有检索到可核对的企业来源",
        )
    fetches = 0
    for url, source in list(sources.items()):
        body = source.content.strip()
        identity_present = bool(
            re.search(r"简称|全称|经营主体|运营主体|注册主体|工商登记名称", body)
            or any(_ENTITY.search(name) and name in body for name in names)
        )
        if fetches >= 2 or (body and (len(body) >= 500 or identity_present)):
            continue
        if deadline - time.monotonic() <= 12:
            unavailable |= not bool(body)
            break
        fetches += 1
        try:
            progress(f"补充企业来源正文（{fetches}/2）")
            fetched = client.fetch(url, deadline - 12)
            # The fetch response cannot change which URL was actually requested.
            if len(fetched.content.strip()) > len(body):
                sources[url] = fetched.model_copy(update={"url": url})
        except DeepSeekError:
            if not body:
                unavailable = True
    evidence = list(sources.values())
    if not any(source.content.strip() for source in evidence):
        return CompanyResolution(
            status="unavailable" if unavailable else "unresolved",
            evidence=evidence,
            reason="未获得可校验的网页正文",
        )
    try:
        progress(f"核实企业候选（{len(evidence)}个来源）")
        suggested = client.resolve_company(extraction, evidence, deadline)
    except DeepSeekError as exc:
        return CompanyResolution(
            status="unavailable",
            evidence=evidence,
            reason="企业核实达到时间上限，保留已获取来源供人工核对"
            if exc.code == "deadline_exceeded"
            else "企业候选分析服务暂不可用",
        )

    def rank():
        ranked = {}
        ambiguous_brand = False
        for candidate in suggested.candidates[:3]:
            source = sources.get(candidate.source_url)
            if source is None:
                continue
            verified, eligible, brand = _score(
                candidate, source, extraction, list(sources.values())
            )
            ambiguous_brand |= brand
            old = ranked.get(verified.name)
            if old is None or verified.score > old[0].score:
                ranked[verified.name] = (verified, eligible)
        return sorted(
            ranked.values(), key=lambda item: item[0].score, reverse=True
        ), ambiguous_brand

    ordered, ambiguous_brand = rank()
    if ordered and not unavailable and not ambiguous_brand and time.monotonic() < deadline:
        best, eligible = ordered[0]
        if best.quote and (not eligible or best.score < 0.85):
            query = " ".join(part for part in (best.name, area, "公司简介 总部地址") if part)[:128]
            try:
                progress("补充核实企业范围与来源")
                for source in client.search(query, deadline)[:5]:
                    if _public_url(source.url) and (
                        source.url not in sources
                        or len(source.content) > len(sources[source.url].content)
                    ):
                        sources[source.url] = source
                ordered, ambiguous_brand = rank()
            except DeepSeekError:
                unavailable = True
    evidence = list(sources.values())
    candidates = [item[0] for item in ordered]
    if unavailable:
        return CompanyResolution(
            candidates=candidates,
            evidence=evidence,
            status="unavailable",
            reason="部分企业检索请求失败，保留已获取证据供人工核对",
        )
    if not ordered:
        return CompanyResolution(evidence=evidence, reason="候选来源或企业全称缺少可核对证据")
    best, eligible = ordered[0]
    margin = best.score - (ordered[1][0].score if len(ordered) > 1 else 0)
    if eligible and best.score >= 0.85 and margin >= 0.15:
        return CompanyResolution(
            selected_name=best.name,
            candidates=candidates,
            status="confirmed",
            evidence=evidence,
            quality_score=best.score,
            reason="企业全称、客户称呼关系和已知范围均有正文证据，候选差距达到要求",
        )
    ambiguous = ambiguous_brand or (len(ordered) > 1 and margin < 0.15)
    return CompanyResolution(
        candidates=candidates,
        status="ambiguous" if ambiguous else "unresolved",
        evidence=evidence,
        quality_score=best.score,
        reason="品牌、加盟或同名主体存在歧义，需人工确认"
        if ambiguous
        else "企业全称或客户主体关系证据不足，无法自动确认",
    )
