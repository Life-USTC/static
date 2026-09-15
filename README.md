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
| `rss/` | 清洗后的校内新闻等 XML 订阅 |
| `bus_data*.json` / `geo_data.json` / `building_img_rules.json` / `feed_source.json` / `imgs/` | 校车、地理、建筑图规则、订阅源元数据与图片 |
| `room_maps.json` / `imgs/rooms/` | 由 `room_map_annotations.json` 生成的、按房间高亮的楼层图 |

房间高亮覆盖 `building_img_rules.json` 引用的 40 张楼层图中明确标注房号的 246 个房间；未标注房号的
空间不推测编号，查询时仍返回楼层概览。房间边界保存在 `static/room_map_annotations.json`，
构建时生成完整楼层图，保留周边位置供定位。

更新源图时需同步检查房间编号、边界坐标和图片尺寸。`uv run python -m unittest discover -s tests -q` 检查规则与标注的楼层对应、生成图片和 5201 的高亮位置。

## 数据从哪来

构建器（`main.py`）按需运行：

- **curriculum** — `catalog.ustc.edu.cn` 与教务课表相关上游 → SQLite
- **young** — `young.ustc.edu.cn` 智慧团学活动 → 写入同一快照库
- **rss** — 校主页新闻、教务处、应用通知等源 → XML；另含体教中心等爬取源

Young 每次构建都会完整刷新进行中和已结束活动列表，并在分页不完整或上游请求失败时保留上一份可用快照。

失败的 builder 会回滚该 builder 的旧产物；`build-status.json` 记录各 builder 状态。
旧的 curriculum JSON 端点与 upstream response cache **已停发**。

## 给贡献者

日更由 GitHub Actions（`build.yml`）驱动。本地与测试约定见仓库内 `tests/` 与
`pyproject.toml`；本 README 只描述产物语义。

本地执行 `uv run python main.py --curriculum` 会在 Pydantic validation 前累计原始
JSON，并在替换 SQLite 和 expected schema 前完成契约检查。Observed schemas 与 report
只写入被 git 忽略、不会发布到 Pages 的 `.artifacts/upstream-contracts/`。使用
`uv run python main.py --verify-upstream-contract` 可强制刷新所有 selected semesters，
获得完整的本地 fetch-context coverage。

上游新增字段、缺失 required 字段和类型不兼容会使 builder 失败。有完整 context
coverage 或至少两个独立 fetch 的多余 optional 才会失败；incremental 单 context 仅提示。
单次抓取也不足以证明长期 nullability；未出现的 nullable / union 分支、始终为 null 的值
和空数组元素类型会明确写入 report 作为 warning，并保留计数供后续人工判断。

## License & Warranty

WE PROVIDE ABSOLUTELY NO WARRANTY. USE THIS SOFTWARE AT YOUR OWN RISK.
