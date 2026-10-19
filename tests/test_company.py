import time

from sdr_agent.company import resolve_company
from sdr_agent.deepseek import DeepSeekError
from sdr_agent.schemas import (
    CompanyCandidate,
    CompanyResolution,
    Evidence,
    Extraction,
    Fact,
    SearchResult,
)

BODY = "杭州青木家居有限公司位于杭州，主营定制家居。青木是杭州青木家居有限公司的简称。"


def extraction(**overrides):
    values = dict(
        company_mention="青木",
        facts={"area": Fact(value="杭州"), "industry": Fact(value="定制家居")},
    )
    values.update(overrides)
    return Extraction(**values)


def candidate(**overrides):
    values = dict(
        name="杭州青木家居有限公司",
        source_url="https://example.com/company",
        quote="杭州青木家居有限公司位于杭州，主营定制家居。",
        relation_quote="青木是杭州青木家居有限公司的简称。",
        score=0,
        area_match=False,
        industry_match=False,
    )
    values.update(overrides)
    return CompanyCandidate(**values)


class FakeClient:
    def __init__(self, sources=None, candidates=None):
        self.sources = (
            [SearchResult(url="https://example.com/company", content=BODY)]
            if sources is None
            else sources
        )
        self.candidates = [candidate()] if candidates is None else candidates
        self.queries = []
        self.fetched = []

    def search(self, query, deadline):
        self.queries.append(query)
        return self.sources

    def fetch(self, url, deadline):
        self.fetched.append(url)
        return next(s for s in self.sources if s.url == url)

    def resolve_company(self, extraction, sources, deadline):
        return CompanyResolution(
            candidates=self.candidates,
            selected_name="幻觉股份有限公司",
            status="confirmed",
            quality_score=1,
        )


def test_confirm_requires_locally_recomputed_body_evidence():
    client = FakeClient()
    result = resolve_company(client, extraction(), time.monotonic() + 30)
    assert result.selected_name == "杭州青木家居有限公司"
    assert result.status == "confirmed"
    assert result.candidates[0].score == 1
    assert 1 <= len(client.queries) <= 2
    assert all("青木" in q and "杭州" in q for q in client.queries)
    assert len(client.fetched) <= 2


def test_llm_cannot_invent_source_or_quote_or_confirm_itself():
    client = FakeClient(candidates=[candidate(source_url="https://invented.com", score=1)])
    result = resolve_company(client, extraction(), time.monotonic() + 30)
    assert result.selected_name is None
    assert result.status == "unresolved"
    client = FakeClient(candidates=[candidate(relation_quote="这条证据不在正文中", score=1)])
    result = resolve_company(client, extraction(), time.monotonic() + 30)
    assert result.selected_name is None
    assert result.candidates[0].score < 0.85


def test_two_equally_supported_companies_remain_ambiguous():
    other = "杭州青木家居股份有限公司"
    text = f"{other}位于杭州，主营定制家居。青木是{other}的简称。"
    sources = [
        SearchResult(url="https://example.com/company", content=BODY),
        SearchResult(url="https://example.com/other", content=text),
    ]
    second = candidate(
        name=other,
        source_url="https://example.com/other",
        quote=text.split("。")[0] + "。",
        relation_quote=text.split("。")[1] + "。",
    )
    result = resolve_company(
        FakeClient(sources, [candidate(), second]), extraction(), time.monotonic() + 30
    )
    assert result.selected_name is None
    assert result.status == "ambiguous"


def test_brand_headquarters_does_not_identify_franchise_customer():
    franchise = "青木加盟店"
    body = "杭州青木家居有限公司位于杭州，主营定制家居。青木加盟店品牌总部为杭州青木家居有限公司。"
    client = FakeClient(
        [SearchResult(url="https://example.com/company", content=body)],
        [candidate(relation_quote="青木加盟店品牌总部为杭州青木家居有限公司。", score=1)],
    )
    result = resolve_company(client, extraction(company_mention=franchise), time.monotonic() + 30)
    assert result.selected_name is None
    assert result.status == "ambiguous"


def test_conflicting_known_area_cannot_be_confirmed_even_with_model_flags():
    client = FakeClient(
        candidates=[
            candidate(area_match=True, industry_match=True, relation_supported=True, score=1)
        ]
    )
    result = resolve_company(
        client,
        extraction(facts={"area": Fact(value="上海"), "industry": Fact(value="定制家居")}),
        time.monotonic() + 30,
    )
    assert result.selected_name is None


def test_empty_results_and_search_errors_are_nonfatal():
    assert (
        resolve_company(FakeClient(sources=[]), extraction(), time.monotonic() + 30).status
        == "unresolved"
    )

    class Broken(FakeClient):
        def search(self, query, deadline):
            raise DeepSeekError("permission_denied", "服务未授权")

    assert resolve_company(Broken(), extraction(), time.monotonic() + 30).status == "unavailable"


def test_only_scoped_reviewed_alias_with_relation_is_accepted():
    alias = dict(
        alias="青木",
        company_name="杭州青木家居有限公司",
        area="杭州",
        industry="定制家居",
        source="https://example.com/review",
        reviewer="审核员",
        relation="已核对营业执照与客户主体一致",
    )
    client = FakeClient(sources=[])
    result = resolve_company(client, extraction(), time.monotonic() + 30, aliases=[alias])
    assert result.selected_name == alias["company_name"]
    assert result.status == "confirmed"
    assert not client.queries
    for changed in [
        {"area": "上海"},
        {"reviewer": ""},
        {"source": ""},
        {"relation": ""},
        {"alias": "青"},
    ]:
        result = resolve_company(
            FakeClient(sources=[]),
            extraction(),
            time.monotonic() + 30,
            aliases=[{**alias, **changed}],
        )
        assert result.selected_name is None


def test_no_company_mention_skips_search_and_candidates_are_capped():
    client = FakeClient()
    assert (
        resolve_company(client, extraction(company_mention=None), time.monotonic() + 30).status
        == "skipped"
    )
    assert not client.queries
    client = FakeClient(candidates=[candidate(name=f"杭州青木{i}有限公司") for i in range(10)])
    assert len(resolve_company(client, extraction(), time.monotonic() + 30).candidates) <= 3


def test_query_never_includes_customer_phone_or_contact_name():
    data = extraction(
        facts={
            "area": Fact(value="杭州"),
            "industry": Fact(value="定制家居"),
            "phone": Fact(value="13800138000"),
            "contact_name": Fact(value="张三"),
        }
    )
    client = FakeClient()
    resolve_company(client, data, time.monotonic() + 30)
    assert all("13800138000" not in q and "张三" not in q for q in client.queries)


def test_explicitly_negated_relation_never_confirms_company():
    relation = "青木并非杭州青木家居有限公司的简称。"
    body = "杭州青木家居有限公司位于杭州，主营定制家居。" + relation
    result = resolve_company(
        FakeClient(
            [SearchResult(url="https://example.com/company", content=body)],
            [candidate(relation_quote=relation)],
        ),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name is None


def test_company_name_location_does_not_override_conflicting_operating_area():
    quote = "杭州青木家居有限公司位于上海，主营定制家居。"
    relation = "青木是杭州青木家居有限公司的简称。"
    result = resolve_company(
        FakeClient(
            [SearchResult(url="https://example.com/company", content=quote + relation)],
            [candidate(quote=quote)],
        ),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name is None


def test_fetch_limited_to_two_and_only_returned_source_urls():
    sources = [SearchResult(url=f"https://example.com/{i}") for i in range(8)]
    client = FakeClient(sources=sources, candidates=[])
    result = resolve_company(client, extraction(), time.monotonic() + 30)
    assert len(client.queries) <= 2
    assert len(client.fetched) == 2
    assert len(result.evidence) <= 10


def test_other_entity_relation_in_same_body_is_not_a_match():
    body = "青木是杭州红叶家居有限公司的简称。杭州青木家居有限公司位于杭州，主营定制家居。"
    result = resolve_company(
        FakeClient(
            [SearchResult(url="https://example.com/company", content=body)],
            [candidate(quote=body, relation_quote=body)],
        ),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name is None


def test_scoped_alias_is_not_reused_for_independent_franchise():
    alias = dict(
        alias="青木",
        company_name="杭州青木家居有限公司",
        area="杭州",
        industry="定制家居",
        source="https://example.com/review",
        reviewer="审核员",
        relation="仅适用品牌总部，不适用加盟商",
    )
    data = extraction(
        company_evidence=[
            Evidence(turn_id=1, speaker="customer", quote="我们是青木的加盟店，独立经营")
        ]
    )
    result = resolve_company(FakeClient(sources=[]), data, time.monotonic() + 30, aliases=[alias])
    assert result.selected_name is None


def test_partial_search_failure_remains_visible_and_unconfirmed():
    class Partial(FakeClient):
        def search(self, query, deadline):
            if self.queries:
                raise DeepSeekError("provider_unavailable")
            return super().search(query, deadline)

    result = resolve_company(Partial(), extraction(), time.monotonic() + 30)
    assert result.status == "unavailable"
    assert result.selected_name is None
    assert result.candidates


def test_franchise_context_cannot_fall_through_to_brand_search_confirmation():
    data = extraction(
        company_evidence=[
            Evidence(turn_id=1, speaker="customer", quote="我们是青木的加盟店，独立经营")
        ]
    )
    assert resolve_company(FakeClient(), data, time.monotonic() + 30).selected_name is None


def test_explicitly_incompatible_primary_industry_blocks_confirmation():
    for business in ["主营餐饮，不从事定制家居", "主营餐饮"]:
        quote = f"杭州青木家居有限公司位于杭州，{business}。"
        result = resolve_company(
            FakeClient(
                [
                    SearchResult(
                        url="https://example.com/company",
                        content=quote + "青木是杭州青木家居有限公司的简称。",
                    )
                ],
                [candidate(quote=quote)],
            ),
            extraction(),
            time.monotonic() + 30,
        )
        assert result.selected_name is None


def test_reviewed_transcription_typo_feedback_restores_company_in_same_scope():
    client = FakeClient(sources=[])
    alias = dict(
        alias="清木",
        company_name="杭州青木家居有限公司",
        area="杭州",
        industry="定制家居",
        source="https://example.com/review",
        reviewer="审核员",
        relation="已核对客户执照与通话：清木是青木的转写错字，同一独立主体",
    )
    result = resolve_company(
        client, extraction(company_mention="清木"), time.monotonic() + 30, aliases=[alias]
    )
    assert result.selected_name == "杭州青木家居有限公司"
    assert not client.queries
