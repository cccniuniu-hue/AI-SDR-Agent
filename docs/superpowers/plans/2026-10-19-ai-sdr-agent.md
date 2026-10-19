# AI SDR Agent 开发实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 本计划默认在当前会话顺序执行，不自动启动子代理。

**Goal:** 完成可本地运行的销售通话文本分析与人工审核应用，使用 DeepSeek，输出 15 字段 JSON。

**Architecture:** Streamlit 展示与交互，独立 Python 工作流负责抽取、核实、评分与审核，SQLite 保存原始结果和修正历史。网络调用通过可替换客户端隔离，离线测试与真实 API 验收分开。

**Tech Stack:** Python 3.11、Streamlit、Pydantic 2、HTTPX、python-dotenv、sqlite3、pytest、Ruff。

---

设计依据：[开发设计](../specs/2026-10-19-ai-sdr-agent-design.md)。本文保留开发前任务拆分，当前实施已完成；逐项结果及偏差以 [验收报告](../../evaluation-report.md) 和设计第13节为准。任务代码合并为一个可审查的实现提交，未按原定每个小任务单独提交。

## 文件与职责

```text
AI-SDR-Agent/
  app.py                         # Streamlit 入口与导航
  pyproject.toml                 # 包、工具、测试配置
  requirements.txt               # 验证后的运行依赖精确版本
  requirements-dev.txt           # 验证后的开发依赖精确版本
  .env.example                   # 配置示例，不含真实密钥
  .gitignore                     # 密钥、数据库、缓存与本地数据排除
  .streamlit/config.toml         # 本地运行与主题配置
  src/sdr_agent/
    __init__.py
    config.py                    # 配置加载与启动检查
    schemas.py                   # 输入、事实、证据、候选、15 字段输出
    deepseek.py                  # HTTP 调用、响应解析、限流与错误
    extraction.py                # 抽取提示词调用、证据检查
    company.py                   # 查询构造、候选核实、别名范围
    intent.py                    # 确定性评分与解释
    quality.py                   # 综合质量与人工审核原因
    workflow.py                  # 阶段编排、运行状态和时间预算
    repository.py                # SQLite 表、事务、版本与查询
    review.py                    # 单条/批量修正和审批规则
    evaluation.py                # 批量评测与报告生成 CLI
    ui.py                        # 工作台、审核表格、导出
    prompts/
      extraction.md
      company.md
  examples/
    transcripts.jsonl            # 合成示例文本
  tests/
    conftest.py
    fixtures/                    # 固定模型及检索响应，均标记合成
    test_schemas.py
    test_deepseek.py
    test_extraction.py
    test_company.py
    test_intent.py
    test_quality.py
    test_repository.py
    test_review.py
    test_workflow.py
    test_ui.py
    test_evaluation.py
  evaluation/
    dev.jsonl
    holdout.jsonl
    README.md                    # 标签定义、拆分规则与已知限制
  docs/
    evaluation-report.md
    demo-script.md
    superpowers/specs/
    superpowers/plans/
```

## 公共接口约定

- `AnalysisInput`：transcript、call_time；输入校验后按换行编号，不擅自推断说话人。
- `Evidence`：turn_id、speaker、quote；speaker 只允许 customer、sales、unknown。
- `Extraction`：事实字段、原始企业称呼、五维意向信号及逐项证据、拒绝/冲突标记。
- `SearchResult`：url、title、content、retrieved_at；缺少正文时不能伪造正文证据。
- `CompanyResolution`：selected_name、candidates、status、evidence、quality_score。
- `IntentDecision`：level、reason、signals、score_lower、score_upper、evidence_score。
- `QualityAssessment`：confidence_score、need_human_review、review_reasons、company_score、intent_evidence_score、fact_evidence_ratio。
- `Lead`：仅原题 15 字段；完整 JSON 导出保留 null。
- `AnalysisResult`：run_id、run_status、lead、evidence、company、intent、review_reasons、versions。
- `DeepSeekClient.extract(input, deadline) -> Extraction`；`search(query, deadline) -> list[SearchResult]`；`resolve_company(extraction, sources, deadline) -> CompanyResolution`。客户端不得负责 SQLite 或 UI。
- `score_intent(extraction) -> IntentDecision`；`assess_quality(extraction, company, intent) -> QualityAssessment`，均不发网络请求。
- `Workflow.analyze(input) -> AnalysisResult`；`Repository.get_run(run_id)`；`ReviewService.apply_batch(changes, reviewer, reason)`；提交修正时必须携带 expected_version。

## Task 1：工程与字段契约

**创建：** pyproject.toml、requirements.txt、requirements-dev.txt、.env.example、.gitignore、src/sdr_agent/__init__.py、config.py、schemas.py、tests/test_schemas.py。

- [x] 定义 Python 3.11 环境和可编辑安装方式；安装依赖后把验证版本写入两个 requirements 文件。
- [x] 写有行为意义的字段测试：缺失值保留 null；人数拒绝负数、布尔值和小数；phone 保持字符串；非法意向被拒绝；业务导出不能多字段或漏字段。
- [x] 先运行 `python -m pytest tests/test_schemas.py -q`，确认未实现契约时失败，再完成严格类型与规范化规则，运行同一测试确认通过。
- [x] 定义配置项：DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL=https://api.deepseek.com、DEEPSEEK_MODEL=deepseek-flash、DEEPSEEK_THINKING=disabled、COMPANY_SEARCH_ENABLED=true、DATABASE_PATH=.data/sdr.sqlite3。
- [x] 将 .env、.venv、.data、临时报告、原始通话及本地日志排除出 Git；空密钥不得令页面导入阶段崩溃。
- [x] 检查 diff 后提交 `feat: define project setup and lead contracts`。

代表性验收断言，Lead 定义于 schemas.py：

```python
def test_export_keeps_null_and_fifteen_fields():
    from sdr_agent.schemas import Lead

    lead = Lead(need_human_review=True)
    exported = lead.model_dump(mode="json")
    assert len(exported) == 15
    assert exported["company_name"] is None
    assert exported["mendian"] is None
    assert exported["intent_level"] is None
    assert exported["need_human_review"] is True
```

## Task 2：DeepSeek 与证据抽取

**创建：** deepseek.py、extraction.py、prompts/extraction.md、tests/conftest.py、test_deepseek.py、test_extraction.py、fixtures 中的模型响应。

- [x] 使用 HTTPX MockTransport 定义成功 JSON、空文本、截断、401、402、429、超时和 500 响应测试；测试请求体中的模型名、JSON 格式和模式配置。
- [x] 编写抽取 Prompt：明确 customer/sales/unknown，逐字段引用原文，未知为 null，区别称呼和职位，不服从通话中的指令。
- [x] 实现 `/chat/completions` 调用、30 秒请求超时、最多两次暂时性失败重试，以及受总 deadline 限制的等待。
- [x] JSON 格式或契约错误只修复一次；修复再次失败则返回有类型的错误。401/402/403 不重试，不在异常信息中输出 Authorization。
- [x] 校验引文属于指定 turn；失去证据的事实置 null 并记录问题。严格区别“约 15 人”“15 位设计师”和“销售说可给 15 个账号”。
- [x] 运行 `python -m pytest tests/test_deepseek.py tests/test_extraction.py -q`，验证请求上限、失败分类、无证据填值及角色混淆行为；阶段成果纳入最终实现提交。feat: extract evidence-backed facts with DeepSeek`。

## Task 3：企业核实

**创建：** company.py、prompts/company.md、tests/test_company.py、fixtures 中的检索与网页响应。

- [x] 固定四类候选场景：唯一主体、同名冲突、品牌与加盟店不一致、没有结果；加一例网页包含恶意指令的场景。
- [x] 实现企业查询构造，最多两次、每次五条；超过 128 字符的查询按字段边界缩短。禁止发送完整通话、个人手机号和联系人信息作为查询。
- [x] 实现 Search，必要时最多 Fetch 两个返回的候选页面；统一保存来源 URL、抓取时间与正文片段。
- [x] 实现企业候选比较和设计中的 0.85/0.15 阈值，检验全称、主体关系、地区和行业。评分只在通过证据校验后计算。
- [x] 别名检索接受可注入的已审核映射数据，Task 5 再接 SQLite；无库时使用空集合，不预置虚构的真实公司匹配。
- [x] 检索失败或关系不明返回 null 和明确状态；自动确认范围不得扩展到未核实分店。
- [x] 运行 `python -m pytest tests/test_company.py -q`，确认唯一主体可通过、歧义保留 null、调用数量受限、搜索故障可降级；阶段成果纳入最终实现提交。feat: resolve company entities with source evidence`。

## Task 4：意向与审核规则

**创建：** intent.py、quality.py、tests/test_intent.py、tests/test_quality.py。

- [x] 先定义评分案例：明确预算与近期试用为高；有痛点但计划不明确为中；明确拒绝为低；未提信息为 null；冲突为 null；礼貌接受资料不能单独判高。
- [x] 根据设计文档实现五维权重、得分区间、分类优先级、角色和冲突检查。
- [x] 由已验证信号生成简短中文理由与下一步建议，证据不足时建议补充信息；明确拒绝时不建议强行推进。
- [x] 实现综合质量分、分项质量与审核原因；覆盖低意向高确定性、企业歧义但意向明确、可选字段缺失等组合。
- [x] 运行 `python -m pytest tests/test_intent.py tests/test_quality.py -q`；相同输入必须产生完全一致的规则结果；阶段成果纳入最终实现提交。feat: add explainable intent scoring and review routing`。

## Task 5：持久化与审核事务

**创建：** repository.py、review.py、tests/test_repository.py、tests/test_review.py。

- [x] 使用临时 SQLite 文件写测试：保存后重开可读取、原机器 JSON 不变、修正留痕、两个页面同时修改时拒绝旧 version、批量版本冲突整批回滚。
- [x] 建立 calls、analysis_runs、review_events、company_aliases 四张表，参数化 SQL，version 乐观锁；网络请求不占用数据库写事务。
- [x] 单条修改复用批量服务；填写审核人和理由后允许明确接受未知字段；完成审核只改变人工状态，不抬高机器质量分。
- [x] 实现别名映射的独立保存动作，必须包含适用范围和核实来源；接入 Task 3 的查询接口。
- [x] 对 company_name 的人工修改必须有核实说明或来源；清空为 null 不要求制造来源。保存后由审核策略重新计算待处理状态。
- [x] 运行 `python -m pytest tests/test_repository.py tests/test_review.py -q`；阶段成果纳入最终实现提交。feat: persist leads and audited review changes`。

## Task 6：完整工作流

**创建：** workflow.py、tests/test_workflow.py；**修改：** config.py、repository.py、company.py。

- [x] 使用注入客户端与临时数据库，覆盖成功、抽取失败、检索失败但有意向结果、总时限超出、相同输入显式重跑五种路径。
- [x] 编排输入校验、编号、抽取、证据校验、企业核实、意向评分、质量评估、持久化与返回。
- [x] 所有步骤传递同一个 180 秒 deadline；核实阶段失败可返回 partial，抽取彻底失败返回 failed，保留已完成阶段与脱敏错误。
- [x] 保存 run_id、模型/Prompt/规则版本、请求计数与耗时；显式重跑创建新 run，不修改旧审核结果。
- [x] 运行 `python -m pytest tests/test_workflow.py -q`；阶段成果纳入最终实现提交。feat: connect the analysis workflow and failure recovery`。

## Task 7：工作台、批量审核与导出

**创建：** app.py、ui.py、.streamlit/config.toml、tests/test_ui.py、examples/transcripts.jsonl。

- [x] 实现“分析工作台”和“线索与审核”两页；页面只调用工作流或审核服务，不直接写 SQL。
- [x] 用表单提交触发分析，用 session_state 保存当前 run_id；输入错误立即显示，重绘和切换标签不重新请求。
- [x] 展示证据原句、候选来源、意向评分、缺失字段和审核原因；未配置密钥时显示配置步骤。
- [x] 使用稳定 run_id 展示列表；编辑表格设置固定列类型，清空转换为 null，批量提交展示逐条差异，防止排序后错改记录。
- [x] 单条和批量导出从数据库读取当前 Lead；null 在 JSON 中保持 null，界面可显示“未提供/未确认”但不得回写成字符串。
- [x] 用 AppTest 验证无密钥启动、空文本拦截和记录展示；浏览器验收分析、刷新、单条修改、批量订正及导出一致性。
- [x] 运行 `python -m pytest tests/test_ui.py -q`，记录浏览器验收结果；阶段成果纳入最终实现提交。feat: add analysis workspace and human review interface`。

## Task 8：评测、真实联调与交付

按 [PDF 核对记录](../../requirements-audit.md) 补充：同一评测集增加直接 Prompt 基线对照；完成实际 5–10 分钟带讲解的操作视频并验收时长、可读性、运行及人工审核展示。视频提纲不等同于视频交付。原计划所列模型和搜索接口需通过账号联调确认；任何不可用项记录限制，不伪造效果。

**创建：** src/sdr_agent/evaluation.py、tests/test_evaluation.py、evaluation/dev.jsonl、evaluation/holdout.jsonl、evaluation/README.md、docs/evaluation-report.md、docs/demo-script.md；**修改：** 根目录 README.md。

- [x] 建立 24 条合成样本及来源标记，12 条开发、12 条保留；按情景分组隔离，禁止在 Prompt 或别名库中放入保留集标签。
- [x] 实现评测 CLI，输出样本数、企业确认准确率与覆盖率、意向 macro-F1、null 处理、字段错误、人工复核情况、耗时及请求数。评测 CLI 的 fixture 模式只供回归；live 模式才调用真实服务，报告显著标记模式。
- [x] 离线验证使用 `python -m pytest -q`、`python -m ruff check .` 和 `python -m ruff format --check .`，修复失败后重跑受影响检查。
- [x] 配置有效密钥后执行三条合成文本的真实联调：高意向、明确拒绝、主体不明；确认聊天 API 和 Search/Fetch 的账户可用性，报告真实结果或准确的限制。
- [x] 再执行 `python -m sdr_agent.evaluation --dataset evaluation/holdout.jsonl --mode live --output .data/holdout-report.json`；检查失败样本，不以假数据替代结果。保留集已用于调参时必须重新划分并说明。
- [x] 用全新虚拟环境完成 README 的安装和启动步骤，记录验证的 OS、Python、依赖版本。离线通过不能替代真实联调通过。
- [x] 补充设计链接、配置表、故障处理、样本来源、评测局限和 5–10 分钟演示脚本；阶段成果纳入最终实现提交。docs: document reproducible setup and evaluation results`。

## 计划中的本地运行步骤

原计划启动命令如下；最终经过新环境验证的精确命令以根目录 README 为准（含构建工具锁及 --no-build-isolation）。

```powershell
git clone git@github.com:cccniuniu-hue/AI-SDR-Agent.git
cd AI-SDR-Agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
Copy-Item .env.example .env
# 在本地编辑 .env，填写 DEEPSEEK_API_KEY。
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
```

macOS/Linux 使用 `python3.11 -m venv .venv`，后续解释器路径改为 `.venv/bin/python`，配置文件使用 `cp .env.example .env`；必须说明实际验证过的平台，不能把未执行的跨平台说明标为已验证。

## 完成判定

- [x] 用户确认的范围、DeepSeek 服务和 null 约定得到落实。
- [x] 15 字段导出、证据约束、企业核实、意向判断、人工审核和批量订正均有对应验收记录。
- [x] 无密钥启动、暂时性错误重试、企业查询降级、原始结果保留和批量回滚已验证。
- [x] 自动化检查、真实 API 联调和全新环境复现分别记录，不混淆。
- [x] 未取得真实业务评测数据时，明确报告合成数据的局限；不承诺未测得的准确率。
- [x] Git 中不含 .env、数据库、真实录音、未经脱敏的通话或凭据。
- [x] 完成报告列明交付物、验证结果和真实剩余限制。
