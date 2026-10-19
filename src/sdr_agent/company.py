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
            or alias.get("alias") != extraction.company_mention
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
    punctuation = r'[\s“”"\u2018\u2019\(\)（）:：]*'
    patterns = [
        alias + punctuation + r"(?:是|为)" + punctuation + full + r"的(?:简称|全称)",
        alias
        + r"(?:的)?(?:公司全称|全称|经营主体|运营主体|注册主体|公司名称)"
        + punctuation
        + r"(?:是|为)?"
        + punctuation
        + full,
        full + punctuation + r"(?:以下)?简称" + punctuation + alias + r"(?![\u4e00-\u9fff])",
        alias + r"(?:门店|店铺)?由" + punctuation + full + r"(?:经营|运营)",
    ]
    return any(re.search(pattern, quote) for pattern in patterns)


def _score(
    candidate: CompanyCandidate, source: SearchResult, extraction: Extraction
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
    relation_valid = bool(
        relation_quote
        and relation_quote in body
        and _direct_relation(mention, candidate.name, relation_quote)
    )
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
    area_match = bool(area and area in cited)
    industry_match = bool(industry and industry in cited)
    industry_conflict = False
    if industry:
        negative = re.search(
            r"(?:不从事|不经营|不是|并非)[^，。；\n]{0,8}" + re.escape(industry), cited
        )
        businesses = re.findall(r"(?:主营|行业为|专注于?|从事)([^，。；\n]+)", candidate.quote)
        tokens = {industry[i : i + 2] for i in range(len(industry) - 1)}
        industry_conflict = bool(negative) or bool(
            businesses and not any(token in business for token in tokens for business in businesses)
        )
        if industry_conflict:
            industry_match = False
    locations = re.findall(
        r"(?:位于|地址\s*[:：]?|注册地\s*[:：]?|经营地\s*[:：]?|所在地\s*[:：]?)\s*([^，。；\n]+)",
        cited,
    )
    if area and locations and any(area not in location for location in locations):
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
            "relation_quote": relation_quote if relation_quote in body else "",
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
    client, extraction: Extraction, deadline: float, aliases: list[dict] | None = None
) -> CompanyResolution:
    mention = extraction.company_mention
    if not mention:
        return CompanyResolution(status="skipped", reason="客户未提供可核对的企业称呼")
    reviewed = _reviewed_alias(extraction, aliases or [])
    if reviewed:
        return reviewed
    area, industry = _fact(extraction, "area"), _fact(extraction, "industry")
    queries = list(
        dict.fromkeys(
            " ".join(part for part in parts if part)[:128]
            for parts in [(mention, area, industry), (mention, area)]
        )
    )[:2]
    sources: dict[str, SearchResult] = {}
    unavailable = False
    for query in queries:
        if time.monotonic() >= deadline:
            unavailable = True
            break
        try:
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
        if source.content.strip() or fetches >= 2:
            continue
        fetches += 1
        try:
            fetched = client.fetch(url, deadline)
            # The fetch response cannot change which URL was actually requested.
            sources[url] = fetched.model_copy(update={"url": url})
        except DeepSeekError:
            unavailable = True
    evidence = list(sources.values())
    if not any(source.content.strip() for source in evidence):
        return CompanyResolution(
            status="unavailable" if unavailable else "unresolved",
            evidence=evidence,
            reason="未获得可校验的网页正文",
        )
    try:
        suggested = client.resolve_company(extraction, evidence, deadline)
    except DeepSeekError:
        return CompanyResolution(
            status="unavailable", evidence=evidence, reason="企业候选分析服务暂不可用"
        )
    ranked = {}
    ambiguous_brand = False
    for candidate in suggested.candidates[:3]:
        source = sources.get(candidate.source_url)
        if source is None:
            continue
        verified, eligible, brand = _score(candidate, source, extraction)
        ambiguous_brand |= brand
        old = ranked.get(verified.name)
        if old is None or verified.score > old[0].score:
            ranked[verified.name] = (verified, eligible)
    ordered = sorted(ranked.values(), key=lambda item: item[0].score, reverse=True)
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
