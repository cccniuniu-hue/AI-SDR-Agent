import time

import pytest

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


def test_company_progress_and_candidate_budget_survive_optional_fetch_timeout():
    messages = []

    class Client(FakeClient):
        def fetch(self, url, deadline):
            self.fetch_deadline = deadline
            raise DeepSeekError("deadline_exceeded")

        def resolve_company(self, extraction, sources, deadline):
            self.candidate_deadline = deadline
            return super().resolve_company(extraction, sources, deadline)

    source = SearchResult(url="https://example.com/company", content="青木的公司介绍。")
    client = Client([source], [])
    deadline = time.monotonic() + 30
    result = resolve_company(client, extraction(), deadline, progress=messages.append)
    assert client.candidate_deadline - client.fetch_deadline >= 12
    assert result.evidence[0].content == source.content
    assert any("检索" in message for message in messages)
    assert any("正文" in message for message in messages)
    assert any("候选" in message for message in messages)


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


@pytest.mark.parametrize(
    "relation",
    [
        "杭州青木家居有限公司（以下简称为“青木”）位于杭州，主营定制家居。",
        "杭州青木家居有限公司，简称为青木，位于杭州，主营定制家居。",
        "青木的工商登记名称为杭州青木家居有限公司。",
        "青木（公司全称：杭州青木家居有限公司）。",
    ],
)
def test_more_explicit_legal_name_relation_phrasings_are_supported(relation):
    source = SearchResult(url="https://example.com/company", content=BODY + relation)
    result = resolve_company(
        FakeClient([source], [candidate(relation_quote=relation)]),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name == "杭州青木家居有限公司"


@pytest.mark.parametrize(
    "alias,name,area,industry,typo",
    [
        ("青木", "杭州青木家居有限公司", "杭州", "定制家居", "清木"),
        ("锦禾", "成都锦禾软件有限公司", "成都", "设计软件", "锦和"),
        ("蓝图", "南京蓝图科技有限公司", "南京", "建筑软件", "兰图"),
        ("星维", "苏州星维技术有限公司", "苏州", "工业软件", "星唯"),
    ],
)
def test_customer_explicit_alias_links_typo_to_sourced_legal_entity(
    alias, name, area, industry, typo
):
    quote = f"{name}位于{area}，主营{industry}。"
    relation = f"{alias}是{name}的简称。"
    source = SearchResult(url="https://example.com/company", content=quote + relation)
    data = extraction(
        company_mention=typo,
        facts={"area": Fact(value=area), "industry": Fact(value=industry)},
        company_evidence=[
            Evidence(turn_id=1, speaker="customer", quote=f"我们叫{typo}，也叫{alias}。")
        ],
    )
    client = FakeClient([source], [candidate(name=name, quote=quote, relation_quote=relation)])
    result = resolve_company(client, data, time.monotonic() + 30)
    assert result.selected_name == name
    assert any(alias in query for query in client.queries)
    assert len(client.queries) <= 3


@pytest.mark.parametrize(
    "speaker,quote",
    [
        ("customer", "我们叫清木，供应商也叫青木。"),
        ("customer", "我们叫清木，不是青木。"),
        ("sales", "清木，也叫青木。"),
        ("customer", "我们叫清木。青木是别的公司。"),
    ],
)
def test_other_party_sales_or_negated_names_cannot_supply_a_correction(speaker, quote):
    data = extraction(
        company_mention="清木",
        company_evidence=[Evidence(turn_id=1, speaker=speaker, quote=quote)],
    )
    result = resolve_company(FakeClient(), data, time.monotonic() + 30)
    assert result.selected_name is None


def test_snippet_is_enriched_before_candidate_validation():
    snippet = SearchResult(url="https://example.com/company", content="青木提供定制家居服务。")

    class Enriching(FakeClient):
        def fetch(self, url, deadline):
            self.fetched.append(url)
            return SearchResult(url=url, content=BODY)

    client = Enriching(sources=[snippet])
    result = resolve_company(client, extraction(), time.monotonic() + 30)
    assert result.selected_name == "杭州青木家居有限公司"
    assert client.fetched == [snippet.url]
    assert result.evidence[0].content == BODY


def test_failed_optional_enrichment_preserves_source_for_human_review():
    snippet = SearchResult(url="https://example.com/company", content="青木提供定制家居服务。")

    class Unavailable(FakeClient):
        def fetch(self, url, deadline):
            raise DeepSeekError("provider_unavailable")

    result = resolve_company(Unavailable(sources=[snippet]), extraction(), time.monotonic() + 30)
    assert result.selected_name is None
    assert result.evidence[0].content == snippet.content


def test_customer_alias_cannot_bypass_franchise_boundary():
    data = extraction(
        company_mention="清木",
        company_evidence=[
            Evidence(
                turn_id=1, speaker="customer", quote="我们叫清木，也叫青木，是独立经营的加盟店。"
            )
        ],
    )
    assert resolve_company(FakeClient(), data, time.monotonic() + 30).selected_name is None


@pytest.mark.parametrize(
    "quote", ["青木是我们的供应商，不是我们公司。", "我们不是青木，只是向他们买软件。"]
)
def test_explicit_non_customer_entity_cannot_be_confirmed(quote):
    data = extraction(company_evidence=[Evidence(turn_id=1, speaker="customer", quote=quote)])
    assert resolve_company(FakeClient(), data, time.monotonic() + 30).selected_name is None


def test_exact_customer_legal_name_does_not_require_a_redundant_alias_quote():
    data = extraction(company_mention="杭州青木家居有限公司")
    result = resolve_company(
        FakeClient(candidates=[candidate(relation_quote="")]),
        data,
        time.monotonic() + 30,
    )
    assert result.selected_name == "杭州青木家居有限公司"
    assert result.candidates[0].relation_quote == candidate().quote


def test_explicit_industry_match_is_not_overruled_by_a_broader_service_description():
    quote = "苏州星维技术有限公司是工业软件供应商，专注于制造业信息化领域，总部位于苏州。"
    relation = "星维是苏州星维技术有限公司的简称。"
    source = SearchResult(url="https://example.com/company", content=quote + relation)
    result = resolve_company(
        FakeClient(
            [source], [candidate(name="苏州星维技术有限公司", quote=quote, relation_quote=relation)]
        ),
        extraction(
            company_mention="星维",
            facts={"area": Fact(value="苏州"), "industry": Fact(value="工业软件")},
        ),
        time.monotonic() + 30,
    )
    assert result.selected_name == "苏州星维技术有限公司"
    assert result.candidates[0].industry_match


def test_same_legal_entity_can_supply_scope_in_a_separate_source():
    relation = "浙江青木家居有限公司（简称为青木）。"
    identity = SearchResult(url="https://example.com/company", content=relation)
    address = SearchResult(
        url="https://example.com/contact",
        content="浙江青木家居有限公司\n总部地址：杭州\n主营定制家居。",
    )
    result = resolve_company(
        FakeClient(
            [identity, address],
            [candidate(name="浙江青木家居有限公司", quote=relation, relation_quote=relation)],
        ),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name == "浙江青木家居有限公司"
    assert result.candidates[0].industry_match
    assert any(
        item.url == address.url and item.content in address.content
        for item in result.candidates[0].scope_evidence
    )


def test_scope_for_a_different_legal_entity_is_not_merged():
    relation = "浙江青木家居有限公司（简称为青木）。"
    other = SearchResult(
        url="https://example.com/contact",
        content="上海红叶家居有限公司\n总部地址：杭州\n主营定制家居。",
    )
    result = resolve_company(
        FakeClient(
            [SearchResult(url="https://example.com/company", content=relation), other],
            [candidate(name="浙江青木家居有限公司", quote=relation, relation_quote=relation)],
        ),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name is None


def test_directory_cannot_lend_another_company_scope_to_the_candidate():
    relation = "浙江青木家居有限公司（简称为青木）。"
    directory = SearchResult(
        url="https://example.com/directory",
        content="浙江青木家居有限公司。上海红叶家居有限公司\n总部地址：杭州\n主营定制家居。",
    )
    result = resolve_company(
        FakeClient(
            [SearchResult(url="https://example.com/company", content=relation), directory],
            [candidate(name="浙江青木家居有限公司", quote=relation, relation_quote=relation)],
        ),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name is None


def test_missing_scope_triggers_one_bounded_legal_name_search():
    relation = "浙江青木家居有限公司（简称为青木）。"
    identity = SearchResult(url="https://example.com/company", content=relation)
    address = SearchResult(
        url="https://example.com/contact",
        content="浙江青木家居有限公司\n总部地址：杭州\n主营定制家居。",
    )

    class Supplement(FakeClient):
        def search(self, query, deadline):
            self.queries.append(query)
            return [address] if "浙江青木家居有限公司" in query else [identity]

    client = Supplement(
        [identity],
        [candidate(name="浙江青木家居有限公司", quote=relation, relation_quote=relation)],
    )
    result = resolve_company(client, extraction(), time.monotonic() + 30)
    assert result.selected_name == "浙江青木家居有限公司"
    assert len(client.queries) == 3


def test_direct_relation_can_be_verified_in_another_source_for_the_same_legal_name():
    intro = "浙江青木家居有限公司位于杭州，主营定制家居。"
    relation = "浙江青木家居有限公司（下称青木）。"
    result = resolve_company(
        FakeClient(
            [
                SearchResult(url="https://example.com/company", content=intro),
                SearchResult(url="https://example.com/legal", content=relation),
            ],
            [candidate(name="浙江青木家居有限公司", quote=intro, relation_quote="")],
        ),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name == "浙江青木家居有限公司"
    assert result.candidates[0].relation_source_url == "https://example.com/legal"


def test_event_city_is_not_a_company_address_in_supplementary_evidence():
    relation = "浙江青木家居有限公司（简称青木）。"
    news = SearchResult(
        url="https://example.com/news",
        content="浙江青木家居有限公司受邀参加杭州家具展，主营定制家居。",
    )
    result = resolve_company(
        FakeClient(
            [SearchResult(url="https://example.com/company", content=relation), news],
            [candidate(name="浙江青木家居有限公司", quote=relation, relation_quote=relation)],
        ),
        extraction(),
        time.monotonic() + 30,
    )
    assert result.selected_name is None
    assert not result.candidates[0].area_match
