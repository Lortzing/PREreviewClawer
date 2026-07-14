# PREreviewClawer

简体中文 | [English](README.md)

PREreviewClawer 是一个分阶段、可复现的数据流水线，用于从 Zenodo 的公开 PREreview 社区收集同行评审数据，并通过 DOI、arXiv 等元数据服务补充被评预印本的信息。

## 为什么采用分阶段流水线？

原有的生产爬虫可以通过一条命令完成全部工作，但研究数据处理通常需要清晰、可检查的中间产物。本项目将流程拆分为四个可独立运行的阶段：

```text
PREreview / Zenodo
        |
        v
01_reviews.json
（评审正文、目标标识符、评审记录、作者回复、讨论线程）
        |
        v
02_metadata.json
（每个目标的 DataCite / Crossref / arXiv 元数据）
        |
        v
03_dataset.csv + 03_dataset_extended.csv
（版本归组、评审轮次、去重、学科字段策略、数据来源）
        |
        v
04_validation.json
（严格的模式与数据质量校验）
```

论文与评审之间的关系只在第一阶段建立，并且只接受 Zenodo `related_identifiers` 中 `relation=reviews` 的显式关系。讨论记录通过 `references`、`cites` 或 `isResponseTo` 显式指向已知评审 DOI，并在第二遍扫描中关联。元数据服务不会参与判断一条评审或讨论属于哪篇论文。

## 环境要求与安装

- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)

安装 uv 后，在项目根目录同步锁定的依赖：

```bash
uv sync
```

如需运行 Jupyter Notebook，请安装可选依赖：

```bash
uv sync --extra notebook
```

`pyproject.toml` 是依赖清单，`uv.lock` 固定完整的解析版本。新增或删除依赖时使用 `uv add`、`uv remove`，不要手动编辑锁文件。所有 Python 命令均应在项目根目录通过 `uv run` 执行。

## 分阶段运行

### 1. 仅收集评审数据

```bash
uv run python scripts/01_collect_reviews.py \
  --max-pages 100 \
  --output data/pipeline/01_reviews.json \
  --stats data/pipeline/01_reviews_stats.json
```

此阶段使用当前 PREreview 官网相同的开放 `prereview-reviews` community endpoint 和评论关联规则，不会请求 Crossref、DataCite、OpenAlex 或 arXiv。评审与评论必须具有明确的 Zenodo 关系，新式讨论以 `comment.html` 为权威正文。对于托管在 Zenodo 的预印本，只有评审记录明确声明 `relation=reviews` 时才会接受。

首次运行或测试配置时，可使用 `--max-pages 2 --allow-partial-scan` 做小规模连通性测试。部分扫描会明确标记为不完整；未指定 `--allow-partial-scan` 时，程序会拒绝静默生成残缺快照。

### 2. 补充 DOI/arXiv 元数据

建议为 Crossref 设置联系邮箱：

```bash
export CROSSREF_MAILTO="your-email@example.com"

uv run python scripts/02_enrich_metadata.py \
  --reviews data/pipeline/01_reviews.json \
  --output data/pipeline/02_metadata.json \
  --stats data/pipeline/02_metadata_stats.json \
  --no-use-openalex
```

阶段输出同时也是检查点。重复运行时，程序会跳过已经记录的目标。使用 `--retry-missing` 可重试未解析的目标。如需重新请求包括已成功解析条目在内的全部目标，应同时使用 `--refresh-metadata` 和 `--no-resume`。

### 3. 组装数据集

```bash
uv run python scripts/03_build_dataset.py \
  --reviews data/pipeline/01_reviews.json \
  --metadata data/pipeline/02_metadata.json \
  --limit 300 \
  --field-policy metadata \
  --sampling-policy coverage
```

默认生成以下文件：

- `03_dataset.csv`：与 F1000 示例兼容的严格八列数据集。
- `03_dataset_extended.csv`：在每轮评审中额外加入 `Target_DOI`。
- `03_audit.json`：字段级数据来源和版本映射记录。
- `03_dedup.json`：组装过程中合并的完全重复评审和讨论记录，同时保留源记录证据。
- `03_build_stats.json`：数据接收与拒绝统计。

### 4. 校验最终 CSV

```bash
uv run python scripts/04_validate_dataset.py \
  --input data/pipeline/03_dataset.csv \
  --audit data/pipeline/03_audit.json \
  --expected 300
```

校验会同时检查 CSV、完整时间线、互动关联和审计证据，结果默认写入 `data/pipeline/04_validation.json`。校验失败时命令以非零状态码退出。

## 一次运行完整流水线

```bash
uv run python scripts/run_pipeline.py \
  --limit 300 \
  --max-pages 100 \
  --field-policy metadata \
  --sampling-policy coverage
```

采集、元数据补充和数据集组装阶段支持缓存或断点续跑，默认缓存和检查点目录为 `data/pipeline/state`。中断后使用相同参数重新执行即可继续。常用刷新参数包括：

- `--refresh-zenodo`：重新获取当前 Zenodo 社区快照。
- `--refresh-metadata`：忽略元数据提供方的缓存；如需同时覆盖第二阶段已经成功解析的条目，须配合 `--no-resume`。
- `--retry-missing`：重试此前未能解析的元数据目标。
- `--no-resume`：不使用已有的续跑状态。

## 字段策略

`--field-policy` 控制最终数据中的 `Field` 字段：

- `empty`：始终写入空字符串。
- `native`：仅使用 PREreview/Zenodo 学科及 arXiv 分类等原始平台分类。
- `metadata`（默认）：在原始平台分类之外，还可使用 DataCite/Crossref 学科信息。
- `broad`：优先采用 `metadata` 策略；仍为空时，再根据标题或期刊进行明确标记的宽泛推断。

推断结果不会被伪装成 PREreview 原生字段，具体来源会记录在审计 JSON 中。

## 抽样策略

`--sampling-policy` 控制符合条件的论文家族如何排序：

- `hash`（默认）：按确定性的 SHA-256 结果排序，避免有意提高多版本论文或含回复/讨论论文的占比。
- `coverage`：优先选择含旧式作者回复或新式讨论的论文家族，其次是多版本论文，再进行确定性补足；适合构建尽可能多的完整 review 轮，但不属于中性抽样。

## 输出模式

严格 CSV 固定包含以下八列：

```text
DOI,PaperTitle,Authors,Source,Venue,Year,PeerReview,Field
```

其中每个 `PeerReview` 轮次包含：

```json
{"Round": 1, "Comments": [{"Reviewer_ID": "10.5281/zenodo.xxxxx", "Reviewer": ["评审者"], "Reviewer_ORCID": [], "Review_Date": "2026-01-01", "Comment": "评审意见正文"}], "Response": [], "Discussion": [], "Timeline": [{"Event_Type": "review", "Event_ID": "10.5281/zenodo.xxxxx", "Actor_Role": "reviewer", "Date": "2026-01-01", "In_Reply_To": ""}]}
```

扩展 CSV 会为每轮评审增加 `Target_DOI`。无论使用哪种 CSV，审计 JSON 都会保留目标标识符及字段来源。

## 完整的 review 轮

每个轮次对应一个被评审的预印本版本，并完整保留：

- `Comments`：该版本的正式 PREreview，包括评审 DOI、评审者、日期和正文。
- `Response`：明确作为作者回复保存的旧式记录。
- `Discussion`：新版“Comment on a PREreview”记录，按发布日期排序，并指向准确的评审 DOI。
- `Timeline`：按时间排列、仅包含事件标识的索引，必须覆盖本轮保留的全部评审、旧式回复和新版讨论。

讨论者身份只按可验证证据判定：优先比较 ORCID，其次比较姓名，最后使用明确的作者回复正文作为兜底。`Comment_Type` 区分 `author_response`、`reviewer_followup` 和 `community_comment`。完全重复的讨论 deposit 只输出一次，但会保留在审计与去重日志中。明确 DOI 关系下的论文改题只记录为警告，不再因此删除已经验证的 review 线程。

中间产物使用 schema v3；旧版本会被明确拒绝并提示重跑前一阶段。项目只声明当前开放 Zenodo community 快照的完整性，不声称 2024-11-12 评论功能重新上线前的旧平台评论已全部迁移。

## Notebook

交互式分析文件位于：

```text
notebooks/prereview_pipeline.ipynb
```

Notebook 调用的函数与命令行脚本相同，并没有维护另一套爬虫实现，因此两种运行方式遵循一致的解析和校验规则。

启动方式：

```bash
uv run jupyter lab notebooks/prereview_pipeline.ipynb
```

## 测试

运行分阶段流水线测试：

```bash
uv run python -m unittest discover -s tests -v
```

运行生产爬虫测试：

```bash
uv run python test_prereview_crawler_production.py
```

测试覆盖序列化往返、目标去重、基于固定中间产物的数据集组装、最终 CSV 校验、版本处理、字段策略和断点恢复等行为。

## 主要模块

- `prereview_crawler_production.py`：解析、规范化、元数据提供方、版本归组、评审去重、作者回复提取及校验。
- `pipeline_stages.py`：阶段边界、中间数据模式、检查点和流程编排。
- `scripts/`：每个阶段的命令行入口以及完整流水线入口。
- `notebooks/`：逐阶段交互运行与检查。
- `README_prereview_crawler_production.md`：原始单体生产爬虫的详细英文说明。

## 数据来源与注意事项

- 评审关系与评审正文来自 Zenodo 的公开 `prereview-reviews` 社区。
- 论文元数据可来自 arXiv、DataCite 和 Crossref；OpenAlex 默认关闭，仅作为可选的最终回退来源。
- 全量采集会访问外部服务并可能耗时较长。建议先以少量页面和较小的 `--limit` 验证环境及输出，再运行完整任务。
- 请遵守各数据服务的使用政策，并合理设置请求间隔；如需联系信息，请设置 `CROSSREF_MAILTO`。
