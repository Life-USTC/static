# Life@USTC Static

为 Life@USTC **server** 准备的上游科大数据快照仓库。不面向终端用户；成功构建后发布到
GitHub Pages，由 server 的静态加载流程导入数据库。

站点根：`https://static.life-ustc.tiankaima.dev/`

## 发布什么

| 产物 | 内容 |
|------|------|
| `life-ustc-static.sqlite` | 规范化后的上游响应（课程 / 课表 / Blackboard 公开课程资料索引等） |
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
- **blackboard** — `www.bb.ustc.edu.cn` 匿名访客会话可见的课程作业 / 实验 / 参考资料
  → 写入同一快照库的 `blackboard_pages` 与 `blackboard_resources`

Young 每次构建都会完整刷新进行中和已结束活动列表，并在分页不完整或上游请求失败时保留上一份可用快照。

失败的 builder 会回滚该 builder 的旧产物；`build-status.json` 记录各 builder 状态。
旧的 curriculum JSON 端点与 upstream response cache **已停发**。

## Blackboard 公开课程资料

抓取范围写在 `blackboard-config.yaml`；每门课程从 `launcher` 页读出课程菜单，再按
content area 递归 listContent，因此新增课程只需写一个 course id。

会话用的是站点自己的匿名入口
`GET /webapps/login?action=guest_login&new_loc=%2Fwebapps%2Fblackboard%2Fexecute%2Flauncher%3Ftype%3DCourse%26id%3D<course_id>`，
全程同一个 HTTP 客户端、跟随跳转、保留 cookie。**不提交用户名和密码**，
`access_mode` 恒为 `guest_session`。

`blackboard_resources` 每行记录请求 URL、最终 URL、状态码、MIME、文件名、字节数、
SHA-256、本地路径、来源页面和分类（`homework` / `lab` / `answer` / `report` /
`slides` / `reference` / `other`）。文件本体只落在 git 忽略、**不发布**到 Pages 的
`.artifacts/blackboard/files/<sha256>` 下（按内容寻址去重）；快照里发布的是元数据索引，
不转载课程文件。带上一次的 `ETag` / `Last-Modified` 做条件请求，304 时沿用已记录的
SHA-256 与大小，不重复下载。

`blackboard_pages` 只把有条目的 listContent 页标成 `indexed = 1`。登录壳页
（`page_kind = login`）、正文哈希重复的页面（`duplicate`）、无条目的导航页
（`navigation`）和「找不到资源」（`not_found`）都记录但不进正文索引。

### 边界

只抓匿名访客本来就能看到的内容。返回 401/403、跳到 `/webapps/login`、或正文是登录表单
的资源一律记为 `access_state = "auth_required"` 并跳过，不重试、不提交凭据、不绕过任何
权限控制。礼貌性约束同样写在配置里：单连接、请求间隔 1.5 秒、遵守 `Retry-After`、
User-Agent 标明项目地址，并有每课程页数 / 资源数 / 单文件大小上限。

## 给贡献者

日更由 GitHub Actions（`build.yml`）驱动。本地与测试约定见仓库内 `tests/` 与
`pyproject.toml`；本 README 只描述产物语义。

课程采集覆盖学校学期列表中的全部学期，不按年份截断课程或考试。已结束学期的
课程与课表、考试分别缓存，只有对应来源完整、每份响应未满 30 天时才复用；未结束
学期每次刷新。缺少考试缓存的旧学期会补抓考试，无需重复抓取仍新鲜的课程和课表。
每次最多并行抓取三个学期。课程列表限时 60 秒，超时或 502/504 时，将该学期声明为
`curriculum_unavailable_semester_ids`，移除其课程、课表和猜测的局部数据，并仅保留真实
失败来源的 `ok=0` 记录；教务课表在既有有限重试耗尽后采用同样规则。服务端应保留这些
学期的生产数据，下一次构建重新尝试。类型校验失败、空响应或返回教学班 ID 与请求
不一致仍会中止整个更新，保留此前产物。

考试接口每学期限时 60 秒。超时或 502/504 会记录失败的 `upstream_fetches`（`ok=0`），
并在 `catalog_exam_unavailable_semester_ids` 中列出，下次运行继续重试。这些学期的考试
不可用于覆盖或删除生产记录；只有成功响应（包括空列表）才有权威性。其他 HTTP 错误、
非 JSON 或类型校验失败仍中止更新。`curriculum_fetch_status=partial_sources_unavailable` 明确表示
部分来源不可用，不等同于全部来源成功。成功与不可用的学期范围必须完整覆盖学校列表，
每个不可用声明必须对应明确失败记录。

`upstream_fetches.fetched_at` 保留每个来源的实际抓取时间。metadata 的
`selected_semester_ids`、`refreshed_semester_ids`、`cached_ended_semester_ids` 描述课程与
课表范围；`catalog_exam_refreshed_semester_ids`、`catalog_exam_cached_semester_ids` 与
`catalog_exam_successful_semester_ids` 分别描述考试请求、缓存和可用范围。
`curriculum_successful_semester_ids` 列出课程与课表都完整的范围。
课程与考试的最小学期 ID 均为 1，`generated_at` 为此次产物完成组装的时间。

本地执行 `uv run python main.py --curriculum` 会在 Pydantic validation 前累计原始
JSON，并在替换 SQLite 和 expected schema 前完成契约检查。Observed schemas 与 report
只写入被 git 忽略、不会发布到 Pages 的 `.artifacts/upstream-contracts/`。使用
`uv run python main.py --verify-upstream-contract` 可强制刷新学校列出的所有学期，
获得完整的本地 fetch-context coverage。

上游新增字段、缺失 required 字段和类型不兼容会使 builder 失败。有完整 context
coverage 或至少两个独立 fetch 的多余 optional 才会失败；incremental 单 context 仅提示。
单次抓取也不足以证明长期 nullability；未出现的 nullable / union 分支、始终为 null 的值
和空数组元素类型会明确写入 report 作为 warning，并保留计数供后续人工判断。

## License & Warranty

WE PROVIDE ABSOLUTELY NO WARRANTY. USE THIS SOFTWARE AT YOUR OWN RISK.
