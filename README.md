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
- **course_substitutions** — 学校公布的成组课程替代关系 → 同一 SQLite 快照
- **course_catalog** — 学校公开课程目录及课程详情 → 同一 SQLite 快照，运行时无需登录
- **section_catalog** — 公开学期信息、开课列表及教学班详情 → 同一 SQLite 快照
- **catalog_groups** — 官网公共基础课、院系与综合素质等目录分组 → 同一 SQLite 快照
- **study_plans** — 学校执行计划、公共课程模块及课程归属 → 同一 SQLite 快照
- **young** — `young.ustc.edu.cn` 智慧团学活动 → 写入同一快照库
- **rss** — 校主页新闻、教务处、应用通知等源 → XML；另含体教中心等爬取源
- **blackboard** — `www.bb.ustc.edu.cn` 匿名访客会话可见的课程作业 / 实验 / 参考资料
  → 写入同一快照库的 `blackboard_pages` 与 `blackboard_resources`

Young 每次构建都会完整刷新进行中和已结束活动列表，并在分页不完整或上游请求失败时保留上一份可用快照。

在同一个 Young 会话中，采集器还会读取一次应用自身的部门树，仅将这两份已验证活动记录实际引用的
`organizer` / `sponsor` 源 ID 对应的精确名称写入可选表 `young_referenced_organization_names`。
完整树只在内存中校验，不发布父链、未引用单位、指导教师、创建者等附加字段。真实数组中的 ID 保持
原始边界；只有上游以逗号拼接的 ID 字符串按其 CSV 约定解析，不拆分名称、不按名称配对或归并同名 ID。

每行通过独立的 `upstream_fetches` 记录保留来源：固定端点、原 HTTP 响应 SHA-256、采集器观察时间和
会话可见范围，不保存令牌或加密查询参数。`young_names_*` 元数据区分当前引用范围、最近尝试状态和
最近成功响应的来源；这些时间不代表上游事件时间，也不会推进或替代 `young_events_synced_at`。
名称仅为这一来源的观察值，不是全校完整组织身份目录，也不改变 server 的主承办方标量、关注或通知。

活动数据健康而部门树失败时，仍可更新活动：当前完整引用范围之外的名称缓存会被移除，范围内未匹配的
旧名称保留原值和原 `fetch_id`，新未知 ID 保持未解析。空树或未命中不是组织删除依据；引用字段本身
部分异常时，仅补充本次明确引用且树中精确命中的名称，不清理旧缓存。元数据中的
`young_names_reference_complete` 和尝试状态明确标记 partial，不声称整个缓存已刷新；新命中行使用新
真实来源，其他行仍保留旧来源。完全没有明确引用时不请求树，保留旧范围并标记未知。
可选新名称写入失败会回滚其 savepoint，不能恢复已清理的无关名称；真正的存储、回滚或最终提交失败
仍会阻止不安全快照发布。此表只覆盖采集记录引用范围，不声称覆盖 server 保留的所有历史活动。

构建器失败时保留原有产物。教学班按学期提交，一个学期失败仍可使用其他学期已验证的更新。`build-status.json` 记录各构建器状态。
旧的 curriculum JSON 端点与 upstream response cache **已停发**。

## 学校课程目录

运行 `uv run python main.py --course-catalog` 单独更新课程资料，默认完整构建也会执行。通过公开搜索的空关键词枚举课程代码，保留历史课程和学校标记为无效的记录。通过 `POST /api/teach/course/infos` 按代码批量读取详情，每批最多 100 个代码，按课程代码匹配返回结果。新课程及摘要发生变化的课程立即刷新；其余详情在距上次获取满 14 天后再次读取，覆盖只改简介或教材的情况。

搜索摘要保存在 `catalog_teach_course_search`，课程详情保存在 `catalog_teach_course_infos`。资料包括开课单位、课程学分和学时、开课季节、评分制、考核方式、学科类别、授课语言、预修要求、中英文简介、教材和参考书，以及学校实际提供的结构化书目和教学大纲。课程代码保留原始字符串与前导零。教材、参考书和考核方式采用学校目录的参考值，具体开课安排可独立变化。

同一命令还会独立刷新官网「课程替代」页，一次读取 `GET /api/teach/course-substitute-pool/list`。表 `catalog_teach_course_substitute_pool_list` 保留每条关系的原课程组和替代课程组。方向、组员和学分均按学校记录保存，多门课合在一起才能构成的替代关系保留为一组。

### 教学班详情

`--course-catalog` 同时采集公开教学班详情，无需校方会话。先读取公开学期与当前公布学期的实际开课列表，再以学期 ID 和班号调用 `POST /api/teach/lesson/infos`，按教学班 ID 和课程 ID 核对返回结果。空库默认采当前公布学期，已有可信开课列表的历史学期也会补采。对于已结束超过 120 天的学期，已有课程采集成功记录时先复用同一官网接口的班号清单，直接批量请求详情。班号、课程或教学班 ID 不符，以及详情缺项时，重新核对官网开课列表。每批最多 100 个班号，每学期最多同时请求 3 批。只采指定学期可运行 `uv run python main.py --section-semester 421`，参数可重复。

已结束超过 120 天的学期，完整资料最多复用 30 天，期满重新核对官网开课列表和详情，以发现班号未变时的书目、大纲等资料修改。新一次成功的课程采集若与公开索引的班号、教学班 ID 或课程 ID 不一致，会在当前运行中重新查询官网开课列表。已确认的缺项或历史接口不可用状态每 7 天重试。

`catalog_teach_public_semester_list` 与 `catalog_teach_public_lesson_list_for_teach` 保留公开学期和开课列表来源，`catalog_teach_lesson_infos` 保存教学班采用的考核方式、预修要求、学分和学时、双语简介、书目和大纲。用 `semester_id` 与班号识别详情。每学期独立提交，缺项与历史不可用状态记在 `catalog_semester_details_{学期ID}` 元数据中，未经验证的部分返回保留原有快照。原有教务课表、排课猜测和考试数据沿用各自来源。

### 官网目录分组与执行计划

`--course-catalog` 和完整构建也会刷新这两组资料。只更新归属和计划时，运行 `uv run python main.py --catalog-relationships`。

- **目录分组**读取官网使用的公共基础课分类、院系和综合素质入口，分别保存入口映射、端点原始回包、分组路径和课程成员。查询 `catalog_teach_course_group_members` 时，可按课程代码反查维度。同一门课在多个来源或分组出现时，各条归属均保留。每次采集从官网当前模块读取入口与端点映射，自动纳入受支持类型的新入口。任一类的入口、端点或映射数量缩小时，会重新读取官网页面和模块，两次解析结果完全一致才接受缩减。结果不一致、重新读取失败或模块结构无法识别时，保留上次成功数据并报告失败。成功读取的模块地址记在快照元数据。
- **执行计划**从公开方案树发现院系、专业、年级和培养类型下的计划 ID，逐一读取计划详情，并沿实际引用读取、去重共享公共模块。`catalog_teach_program_tree`、`catalog_teach_program_info`、`catalog_teach_course_module_info` 保存原始响应，`catalog_teach_program_courses` 将课程与具体方案、模块、必选属性和建议学期关联。计划和模块详情可复用 14 天内的成功记录。 [官网接口调查](docs/school-catalog-interfaces.md)

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

## Pages 产物与增量缓存

本地 `build/life-ustc-static.sqlite` 包含完整数据。Pages 打包另建发布目录，发布两份快照：

| 文件 | 内容 |
| --- | --- |
| `life-ustc-static.sqlite.gz` | 完整 SQLite 的 gzip，解压后与本次构建原文件逐字节相同 |
| `life-ustc-static.sqlite` | 原下载地址的 SQLite，保留教务课表、已有考试、Young 和 Blackboard 等既有数据。新增公开课程和教学班详情、替代关系、目录归属和执行计划通过完整压缩快照获取 |

`tools.pages_snapshot prepare` 保留本地构建库、排除发布诊断文件，并核对发布目录总大小。下一次构建通过 `restore` 优先恢复完整压缩库。早期只有原始 SQLite 的 artifact 仍可恢复。压缩库损坏时会尝试更早的 artifact，避免将当次兼容库当作完整缓存。

## 给贡献者

日更由 GitHub Actions（`build.yml`）驱动。本地与测试约定见仓库内 `tests/` 与
`pyproject.toml`；本 README 只描述产物语义。

课程采集覆盖学校学期列表中的全部学期，不按年份截断课程或考试。已结束学期的
课程与课表、考试分别缓存，只有对应来源完整、每份响应未满 30 天时才复用；未结束
学期每次刷新。缺少考试缓存的旧学期会补抓考试，无需重复抓取仍新鲜的课程和课表。
每次最多并行抓取三个学期。课程列表限时 60 秒，超时、连接/读写或远程协议错误、502/504 时，将该学期声明为
`curriculum_unavailable_semester_ids`，移除其课程、课表和猜测的局部数据，并仅保留真实
失败来源的 `ok=0` 记录；教务课表在既有有限重试耗尽后采用同样规则。服务端应保留这些
学期的生产数据，下一次构建重新尝试。类型校验失败、空响应或返回教学班 ID 与请求
不一致仍会中止整个更新，保留此前产物。

考试接口每学期限时 60 秒。超时、连接/读写或远程协议错误、502/504 会记录失败的 `upstream_fetches`（`ok=0`），
并在 `catalog_exam_unavailable_semester_ids` 中列出，下次运行继续重试。这些学期的考试
不可用于覆盖或删除生产记录；只有成功响应（包括空列表）才有权威性。其他 HTTP 错误、本地请求协议错误、
非 JSON 或类型校验失败仍中止更新。`curriculum_fetch_status=partial_sources_unavailable` 明确表示
部分来源不可用，不等同于全部来源成功。成功与不可用的学期范围必须完整覆盖学校列表，
每个不可用声明必须对应明确失败记录。

`upstream_fetches.fetched_at` 保留每个来源的实际抓取时间。metadata 的
`selected_semester_ids`、`refreshed_semester_ids`、`cached_ended_semester_ids` 描述课程与
课表范围；`catalog_exam_refreshed_semester_ids`、`catalog_exam_cached_semester_ids` 与
`catalog_exam_successful_semester_ids` 分别描述考试请求、缓存和可用范围。
`curriculum_successful_semester_ids` 列出课程与课表都完整的范围。
课程与考试的最小学期 ID 均为 1，`generated_at` 为此次产物完成组装的时间。

SQLite schema 6 始终包含 `jw_room_types`（可为空）。课表中的教室对象提供常见类型，
教学班仅提供 `roomTypeId`；缺少描述的引用从教务 lesson-search 的真实 `roomType` 对象
补齐，保留学期和 fetch provenance。补充字典每次构建重新抓取，只访问仍缺类型的学期；
分页不完整、类型冲突或仍有未解析引用会阻止发布。该表只存类型描述，不保存查询账号路径。

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
