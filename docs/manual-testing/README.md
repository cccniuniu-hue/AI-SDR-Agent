# AI SDR Agent 手工验收手册

编写日期：2026-10-19。应用基线：`9259c9d`；执行时请另记 `git rev-parse --short HEAD` 的实际版本。

这里提供 **89 条手工用例、22 段通话样例和可填写的 Excel 执行表**。所有用例初始状态均为“未执行”，不是本轮已通过的测试报告。用例依据原《AI应用开发实习生笔试题目.pdf》4 页要求、已确认的范围变更和当前产品功能编写。

- [完整用例：前置条件、操作步骤与预期结果](cases.md)
- [通话样例：复制代码块即可](test-data.md)
- [Excel 执行记录表](../../outputs/manual-acceptance-20261019/manual-test-cases.xlsx)
- [项目安装说明](../../README.md)、[架构与实现](../architecture.md)、[既有评测报告](../evaluation-report.md)

## 1. 验收范围与 PDF 对照

| 要求编号 | 来源和验收目标 | 覆盖用例 |
| --- | --- | --- |
| R01 | PDF 第1–2页：输入通话转写文本，提取客户线索 | B01–B10、C02–C09 |
| R02 | PDF 第2页：输出指定15字段；缺失信息统一 `null` | C01–C10、F06–F07、H01–H04 |
| R03 | PDF 第1、3页：口语简称/错字还原企业全称，可核实、避免错认（评分30%） | D01–D08、I03–I06 |
| R04 | PDF 第1、4页：高/中/低意向与依据，信息不足不能瞎猜（评分30%） | E01–E07、C05、C08–C09 |
| R05 | PDF 第3–4页：抽取、核实、评分、输出工作流及异常处理（评分20%） | B04、J01–J09、A06 |
| R06 | PDF 第1、3–4页：轻量 Web 管理、低置信度人工审核 | F01–F13、H05–H08、K01–K02 |
| R07 | PDF 第4页：快速修改企业/意向，支持批量订正 | G01–G09 |
| R08 | PDF 第1、4页：保留人工反馈，支持后续优化 | F11、I01–I06 |
| R09 | PDF 第3–4页：GitHub 源码、设计文档、本地可复现与评测材料 | A01–A06、K03 |
| R10 | 已确认的界面约定：日期可选、年左月右、时分可选、创建时间整秒无偏移后缀 | B05–B07、F13、H07 |

已确认的范围：只输入文本；使用 DeepSeek；不支持音频上传或转写；不接真实 CRM；交付 GitHub 仓库和本地运行说明；不录制演示视频。原 PDF 视频要求按用户约定不适用，不计为漏测。未知意向可为 `null`，人数为非负整数或 `null`；这两点以已确认的数据契约为准。

PDF 没有规定最低准确率，不在本手册中另造百分比门槛。D01–D03 是企业效果正例：只有正确主体和充分来源一起成立才算命中。保守留 `null` 可以符合安全规则，但该正例的还原目标仍未达成，执行表记“失败”，实际结果注明“未覆盖”。品牌或加盟关系不明的反例则应留 `null`。

## 2. 如何记录结果

Excel“执行记录”的浅黄色区域用于填写状态、实际结果、记录编号/版本、证据。其他两页提供完整步骤和可复制文本。建议另存一份带日期和执行人姓名的工作簿，保留本模板。

| 状态 | 使用方式 |
| --- | --- |
| 未执行 | 尚未运行，不等于通过 |
| 通过 | 本用例所有步骤及预期均满足；多场景用例必须逐场景检查 |
| 失败 | 行为与预期不符，包括正例未识别；记录具体差异 |
| 阻塞 | 无法完成验证，例如密钥无效、网络不可用、必需前置条件缺失；写明原因 |
| 不适用 | 本次环境确实不适用，并说明依据；不能用于隐藏失败 |

P0 表示核心验收项；P1 表示补充、边界或可用性项。它们是本手册安排执行顺序的建议，不是 PDF 额外评分标准。多个子场景中只要一个失败，整条不能标通过；可在“实际结果”逐项填写，例如 `401通过 / 402通过 / 403失败 / 404未做`。

模型输出措辞可以不同。判定要看事实、否定、角色、来源和类型是否正确，不能只比对一句固定理由。人工改正后的值不计为模型首次识别正确。若是已知限制，也要按实际结果填写。

## 3. 准备独立测试环境

日常应用仍在 `http://127.0.0.1:8501/`。以下测试在其他端口和 `.data/manual-tests/` 的独立数据库中运行，避免改动日常线索和别名。

先按项目 README 安装依赖和 editable 项目。在每个新 PowerShell 窗口执行下面的公共准备。首次克隆到其他位置时，只需改第一行路径。

```powershell
Set-Location 'E:\群核科技ai coding\AI-SDR-Agent'
if (Test-Path '.\.venv\Scripts\python.exe') {
    $testPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
} elseif (Test-Path '.\.data\repro-venv\Scripts\python.exe') {
    $testPython = (Resolve-Path '.\.data\repro-venv\Scripts\python.exe').Path
} else {
    throw '请先按 README 创建虚拟环境并安装项目'
}
$env:PYTHONIOENCODING = 'utf-8'
New-Item -ItemType Directory -Force '.\.data\manual-tests' | Out-Null
git rev-parse --short HEAD
```

### F：不用模型的预置数据环境

适用于审核、批量、筛选、导出和时间编辑。执行以下命令后打开 `http://127.0.0.1:8502/`：

```powershell
& $testPython scripts/manual_test_tools.py seed --database .data/manual-tests/fixture-20261019.sqlite3
$env:DATABASE_PATH = Join-Path (Get-Location).Path '.data/manual-tests/fixture-20261019.sqlite3'
$env:DEEPSEEK_API_KEY = ' '
& $testPython -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502 --server.headless true
```

密钥变量故意是**一个空格**，应用会去掉空格后判断为未配置。不要改成空字符串：某些 PowerShell 版本会删除空环境变量，进而重新读取 `.env` 中的真实密钥。

`seed` 只允许在本项目 `.data/manual-tests/` 内创建**新文件**，文件存在就拒绝覆盖。重新开始测试时，把命令和 `DATABASE_PATH` 中的文件名同时改成 `fixture-20261019-v2.sqlite3`。只重启已有测试库时跳过 `seed`。

| 固定记录编号 | 初始意向 | 创建时间 | 初始状态 |
| --- | --- | --- | --- |
| manual-high | high | 生成时的1天前 | 分析完成、未人工审核 |
| manual-medium | medium | 生成时的8天前 | 分析完成、未人工审核 |
| manual-low | low | 生成时的31天前 | 分析完成、未人工审核 |
| manual-unknown | null，企业也为null | 生成时的1天前 | 分析完成、未人工审核 |
| manual-failed | 无有效线索 | 生成时的1天前 | 分析失败 |

4 条正常记录的初始版本均为1，质量分为0.5，通话时间为 `2026-10-18T06:30:45.123456+00:00`；企业名和意向标签是人工预置，不能用来证明模型效果。先做 H05/H06，再修改记录，否则状态统计会变化。测试库使用一周后请重新生成，以免相对日期改变筛选预期。

### L：真实 DeepSeek 环境

适用于事实抽取、企业核实、意向判断和反馈应用。停止占用8502的 F 服务，另开一个 PowerShell 窗口执行公共准备，再执行：

```powershell
$env:DATABASE_PATH = Join-Path (Get-Location).Path '.data/manual-tests/live-20261019.sqlite3'
$env:COMPANY_SEARCH_ENABLED = 'true'
& $testPython -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502 --server.headless true
```

真实密钥从本地 `.env` 读取，不需要写入用例或截图。不要在模拟器窗口中直接启动 L 环境，以免继承模拟地址或假密钥。确认侧栏模型、企业查询状态和数据库路径后再分析。真实调用会消耗 API 额度，可复用同一次分析检查多个用例。

单条/批量审核测试也可使用 L 环境结果，但要先保存未编辑的 JSON、原文、证据和 run_id。企业正例 D01–D03 要在添加相关人工别名前执行。

### M：本地异常模拟环境

这组命令不访问 DeepSeek，使用假的本地密钥。开两个新 PowerShell 窗口，分别执行公共准备。

窗口一启动模拟器：

```powershell
& $testPython scripts/manual_test_tools.py serve --scenario 401 --port 8877
```

窗口二启动测试应用，然后打开 `http://127.0.0.1:8503/`：

```powershell
$env:DATABASE_PATH = Join-Path (Get-Location).Path '.data/manual-tests/mock-20261019.sqlite3'
$env:DEEPSEEK_BASE_URL = 'http://127.0.0.1:8877'
$env:DEEPSEEK_API_KEY = 'manual-test-only'
$env:DEEPSEEK_MODEL = 'manual-fixture'
$env:COMPANY_SEARCH_ENABLED = 'true'
& $testPython -m streamlit run app.py --server.address 127.0.0.1 --server.port 8503 --server.headless true
```

每个子场景在窗口一按 `Ctrl+C` 停止，再用对应 `--scenario` 重启，确保计数从1开始。窗口二应用一般无需重启。每个场景只点击一次“开始分析”，使用 S21 原文，观察窗口一的路径和 `request=N`；不会输出密钥或通话内容。

| scenario | 对应用例 | 预期请求及结果 |
| --- | --- | --- |
| ok | 恢复验证 | chat 1次；返回预设空事实的正常结果 |
| 401 / 402 / 403 / 404 | J01，各执行一次 | chat 各1次；失败并显示对应原因 |
| retry-once | J02 | chat 2次；首个429后恢复成功 |
| 429 / 503 | J03，各执行一次 | chat 各3次；重试耗尽后失败 |
| timeout | J04 | 每次延迟35秒；通常约90秒加退避后失败 |
| malformed-once | J05 | chat 2次；一次 JSON 修复后成功 |
| malformed-always | J06 | chat 2次；修复仍无效后失败 |
| search-failure | J07 | chat 1次、search 3次；保留杭州/全屋定制，企业为null，部分完成 |
| 不启动模拟器 | J09 | 连接失败；恢复 ok 后可重新分析 |

J08 改的是窗口二的 `COMPANY_SEARCH_ENABLED`：先停应用，设为 `'false'` 后重新运行；测试后恢复 `'true'` 并重启。模拟结果只用于故障处理测试，不作为抽取准确性或企业识别效果。

测试结束分别 `Ctrl+C` 停止测试服务、关闭这些 PowerShell 窗口，日常8501应用继续使用原库。测试数据保留在 `.data/manual-tests/`，不提交 Git。

## 4. 执行顺序

先做以下冒烟清单，发现核心流程阻塞就先记录原因。冒烟通过后仍需完成其余用例，不能视为89条全部通过。

1. F 环境：A02 → H05 → H06 → F01 → F02 → F09 → F13 → G02 → G03 → H01 → H02。
2. L 环境：B04 → C01 → C06 → D01 → D04 → E01 → E02 → E03 → E04。
3. M 环境：J01 → J02 → J05 → J06 → J07。

完整测试建议按环境分组执行：F 环境先做初始状态检查，再做单条/批量修改和冲突；L 环境先分析样例并留存机器原值，再做企业别名反馈；M 环境逐场景重启模拟器。每完成一个场景就记录，不依赖最后回忆。

用例存在数据依赖：F13 的第一步须使用尚未改过通话时间的预置记录；G05 前要用 F02 把两条记录设置为**不同且已知的企业与意向**，不能沿用 G04 清空后的值；I03–I06 必须在同一个隔离库进行。F、L 库不会自动同步别名。

标签为“真实 DeepSeek”的用例必须使用 L 环境。其他 UI 操作用例优先使用 F 环境；输入提交的空值拦截需要可点击分析按钮，可在 L 或 M 中检查。用例前置条件优先于默认环境分类。

## 5. 15 字段判定表

下载“本条 JSON”应得到一个对象；“导出所选 JSON”应得到对象数组，每个对象恰好包含下列15字段。表格自带 CSV 下载不是业务 JSON 导出。

| 字段 | 类型 | 核对重点 |
| --- | --- | --- |
| company_name | string / null | 有证据的实际客户企业全称；不能仅凭品牌或供应商匹配 |
| industry | string / null | 客户已确认的行业，不拿销售猜测补齐 |
| area | string / null | 客户已确认地区，未知不使用销售所在地 |
| contact_name | string / null | 姓名或确认称呼；“张总”不扩写姓名 |
| contact_title | string / null | 明确职位；“张总”不能推断董事长 |
| phone | string / null | 保留原文脱敏字符，不补造号码 |
| intent_level | high / medium / low / null | 高、中、低映射正确；信息不足或冲突为null |
| intent_reason | string / null | 与实际信号相符，能对照原文或人工核实说明 |
| demand_summary | string / null | 客户需求摘要，不编造规模、预算或承诺 |
| mendian | 非负 integer / null | 明确0保留0，未知/近似数为null，不接受小数 |
| shejishi | 非负 integer / null | 同上；PDF原例“大概15人”不作精确15 |
| call_time | 带时区的 ISO 8601 string / null | UI按北京时间输入；未填写为null，不以创建时间替代 |
| next_action | string / null | 下一步是建议；不得伪装客户承诺，明确拒绝时尊重拒绝 |
| confidence_score | 0–1 number / null | 证据质量指标，不是购买概率；人工不能覆盖 |
| need_human_review | boolean | 企业/意向未知或质量不足需复核；人工明确接受后可为false |

空值指 JSON 的 `null`，不是字符串 `"null"`、`"未知"` 或空字符串。网页用“待核实”“—”表示空值是显示行为，应以 JSON 类型复核。

通话时间和创建时间是不同字段：`call_time` 的 JSON 保留时区；列表/审核轨迹中的创建时间显示到秒且隐藏偏移，当前实现保留 UTC 数值，不自动加8小时。不要因隐藏 `+00:00` 就按北京时间解读创建时间。

## 6. 长度边界数据

公共准备后执行下列命令生成 B08 的两个文本文件。无额外换行，末尾的“终”用于观察粘贴是否完整。只验证输入限制，不必分析这些无业务意义的长文本。

```powershell
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path (Get-Location).Path '.data/manual-tests/length-20000.txt'), ('甲' * 19999) + '终', $utf8NoBom)
[IO.File]::WriteAllText((Join-Path (Get-Location).Path '.data/manual-tests/length-20001.txt'), ('甲' * 20000) + '终', $utf8NoBom)
```

## 7. 问题记录与复测

发现问题时保留：用例编号、代码版本、F/L/M环境、样例编号、操作步骤、预期与实际、run_id/修改前后版本、截图或下载JSON。真实模型结果额外记录模型名、查询开关、分析时间和来源URL；不记录密钥。需要多人协作时可复制以下模板：

```text
用例编号：
执行人 / 日期：
代码版本 / 环境 / 浏览器：
样例及变体：
记录编号 / 版本：
操作步骤：
预期：
实际：
附件路径：
复现次数 / 尝试次数：
修复版本及复测结果：
```

最终汇总分别列出通过、失败、阻塞、未执行、不适用数量，以及 P0 中的未完成项。企业正例区分正确命中、未覆盖和错认；意向按 high/medium/low/null 分别核对。保留第一次机器结果，不用人工订正后的结果替换原始评测记录。截图和 JSON 足以作为手工验收证据，无需视频。
