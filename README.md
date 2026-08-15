# Life@USTC Static

为 Life@USTC **server** 准备的上游科大数据快照仓库。不面向终端用户；成功构建后发布到
GitHub Pages，由 server 的静态加载流程导入数据库。

站点根：`https://static.life-ustc.tiankaima.dev/`

## 发布什么

| 产物 | 内容 |
|------|------|
| `life-ustc-static.sqlite` | 规范化后的上游响应（课程 / 课表等） |
| `life-ustc-static-guesses.sqlite` | 无法直接从上游键出的推断关系 |
| `schemas/upstream/*.expected.schema.json` | 从 Pydantic 生成的上游契约 |
| `schemas/upstream/*.observed.schema.json` | 本次 curriculum 完整抓取反推的确定性契约与计数 |
| `schemas/upstream/contract-report.json` | expected / observed 交叉检查策略及非阻断提示 |
| `rss/` | 清洗后的校内新闻等 XML 订阅 |
| `bus_data*.json` / `geo_data.json` / `building_img_rules.json` / `feed_source.json` / `imgs/` | 校车、地理、建筑图规则、订阅源元数据与图片 |

## 数据从哪来

构建器（`main.py`）按需运行：

- **curriculum** — `catalog.ustc.edu.cn` 与教务课表相关上游 → SQLite
- **young** — `young.ustc.edu.cn` 智慧团学活动 → 写入同一快照库
- **rss** — 校主页新闻、教务处、应用通知等源 → XML；另含体教中心等爬取源

失败的 builder 会回滚该 builder 的旧产物；`build-status.json` 记录各 builder 状态。
旧的 curriculum JSON 端点与 upstream response cache **已停发**。

## 给贡献者

日更由 GitHub Actions（`build.yml`）驱动。本地与测试约定见仓库内 `tests/` 与
`pyproject.toml`；本 README 只描述产物语义。

本地执行 `uv run python main.py --curriculum` 会在 Pydantic validation 前累计原始
JSON，并在替换 SQLite 和 schema 产物前完成契约检查。上游新增字段、缺失 required
字段、类型不兼容，以及有充分 object-instance 证据的多余 optional 都会使 builder
失败并保留上一份产物。单次抓取不足以证明长期 nullability；未出现的 nullable / union
分支、始终为 null 的值和空数组元素类型会明确写入 report 作为 warning，并保留观测
计数供后续人工判断。

## License & Warranty

WE PROVIDE ABSOLUTELY NO WARRANTY. USE THIS SOFTWARE AT YOUR OWN RISK.
