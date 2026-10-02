# BBC + 新浪财经 + Financial Times 新闻共享库

这是一个每天自动采集、可用 Git 同步的 SQLite 新闻库。每天**北京时间 20:00**开始采集 BBC、新浪财经和 Financial Times，按原文 URL 去重，**三个来源累计达到 3000 篇正文后停止新增**。

初始数据为 **181 篇**（BBC 23 篇、新浪财经 158 篇），原始采集窗口为北京时间 **2026-09-25 21:07:26 至 2026-09-27 21:07:26**。此后每次查找最近 48 小时发布的新文章，保留以往已收集的数据。当前总量、剩余数量和下次计划时间见 [data/progress.json](data/progress.json)。

## 自动采集

每天北京时间 **20:00** 开始，UTC 为 **12:00**，每轮只新增最近 48 小时发布的文章。三个来源合计达到 **3000 篇唯一正文**后停止新增；已有数据保留。网站没有足够文章时不会生成填充数据。

- **BBC、新浪财经：** GitHub Actions 云端采集，不依赖个人电脑。
- **Financial Times：** 在已授权的本机环境采集，每轮最多 100 篇；本机自动任务通过 GitHub 连接器上传文章批次，云端验证后合并。电脑须联网、保持开机且 Codex 正在运行；关闭或休眠时无法本地采集，恢复运行后可补一次最近计划周期。
- 两条云端写入工作流使用同一个并发锁。FT 批次进入独立的接收分支，云端取得最新主库后追加、去重，保留 BBC、新浪和历史 FT 数据。不会用旧本机数据库覆盖云端主库。
- GitHub 调度及本机任务可能有延迟，不能保证整点立即启动。当前数量与剩余条数见 [data/progress.json](data/progress.json)。

运行入口：[BBC／新浪每日采集](https://github.com/jonehandson11-spec/News-AI-quant-project/actions/workflows/crawl.yml)、[本地 FT 批次导入](https://github.com/jonehandson11-spec/News-AI-quant-project/actions/workflows/import-ft.yml)。云端每日任务的 **probe** 只验证 BBC／新浪的临时数据库；**crawl** 立即正式采集这两个来源。

配置见 [crawl_config.json](crawl_config.json)。FT 本地程序见 [scripts/ft_sync.py](scripts/ft_sync.py)，批次验证与合并见 [scripts/ft_local.py](scripts/ft_local.py)。本地状态和凭据目录必须在 Git 仓库外；上传仅限准备程序列出的文章批次，不上传 Cookie 或本机日志。只有在主库报告中核验批次回执后才确认完成；上传失败或导入被取消时保留原批次，用同一批次重试。

需要手动补录最近五天的 FT 时，在同一 `prepare` 命令中明确加上 `--lookback-hours 120`。此模式可在当天自动采集已执行后运行，但仍先恢复未确认批次，每批最多 100 篇、三个来源合计最多 3000 篇。每批须核验主库回执并执行 `acknowledge` 后才能继续。默认每日采集窗口仍是 48 小时；补录不会更改定时计划。补录只接受符合真实发布时间窗口且获准读取的正文，发现范围或访问权限不足时不能保证补满 3000 篇。

## FT 登录与长期运行

FT 在 GitHub 云端曾返回 HTTP 403（access_denied），同一文章在本机可读。此次改为本地采集、云端合并，云端任务不再请求 FT 页面。403 不等同于 Cookie 过期。

FT 从 13 个分类 RSS 发现文章，并核对网页原始发布时间和正文。只保存账户获准读取的正文，不把 RSS 摘要或订阅提示当作新闻。FT Cookie 保存在仓库外的本机私密文件中，由本地程序读取；当前工作流不使用 GitHub FT_COOKIE Secret。不要将账号、密码、Cookie 或浏览器状态文件提交到仓库。

五天手动补录还会读取上述分类的可见历史列表，最多请求 40 个分类页面、每类最多 5 页，遇到访问限制即停止。分类列表只用于发现链接，最终仍以文章原始发布时间为准。报告中的 `discovery.coverage_limited` 表示这不是完整 FT 档案索引；不能把“本轮发现的文章已处理”解释为“FT 五天全部文章已收齐”。

会话无法保证永久有效。最近一次本地采集结果见 [data/source_reports/ft.json](data/source_reports/ft.json)，其中 execution_location 为 local，batch_id 用于核对已合并的批次。云端每日任务对 FT 的 collected_locally 跳过不会覆盖本地健康状态。没有新文章不代表已验证登录仍有效。

- auth_expired：会话过期或 HTTP 401。
- auth_required／login_or_subscription_required：缺少登录或订阅验证未通过。在浏览器正常登录并确认文章可读后，更新本机私密 Cookie 文件，再运行本地程序验证；仅在浏览器登录不会自动改写该文件。
- access_denied、rate_limited、网络或解析故障分别记录，不推断为 Cookie 过期，不绕过网站访问限制。

本机的 FT 监控在出现新的可处理故障时提醒；同一未变化故障不重复提醒，恢复后通知一次。历史正文不会因登录失效被删除。

## 下载和在 DBeaver 中打开

1. 打开 [data/news.sqlite3](data/news.sqlite3)，使用 GitHub 文件页的 **Download raw file** 下载原始文件。也可以点击仓库的 **Code → Download ZIP**，解压后使用其中的 `data/news.sqlite3`。
2. 在 DBeaver 中选择 **新建数据库连接 → SQLite**。
3. 在数据库文件路径中选择下载的 `news.sqlite3`，完成连接。SQLite 无需服务器地址、账号或密码。
4. 展开 **Tables → news**，右键查看数据；或者打开 [queries.sql](queries.sql) 执行常用查询。

也可下载 [data/news.csv](data/news.csv)，用 Excel、文本编辑器或其他分析工具打开。CSV 使用带 BOM 的 UTF-8，包含与 SQLite 相同的八个字段及全文；GitHub 能否直接预览取决于文件大小和页面限制。

## 共享方式

仓库地址：[jonehandson11-spec/News-AI-quant-project](https://github.com/jonehandson11-spec/News-AI-quant-project)。本快照放在已有的共享项目中，沿用项目原有成员设置。当前仓库公开，任何人都可查看和下载；写入权限由项目所有者在 [Settings → Collaborators](https://github.com/jonehandson11-spec/News-AI-quant-project/settings/access) 管理。

GitHub 保存的是**版本化文件快照**，不是可让多台电脑直接连接并同时执行 SQL 读写的数据库服务。每位共享成员下载或 clone 仓库后，在自己的电脑上用 DBeaver 打开 SQLite 文件。

可以用 `git clone https://github.com/jonehandson11-spec/News-AI-quant-project.git` 获取副本，后续用 `git pull` 同步新版本。更新前关闭 DBeaver 对该文件的连接，并将个人修改保存在另外的数据库副本中，避免覆盖或产生二进制文件冲突。

若以后需要多人实时查询同一个在线数据库，可将数据导入 PostgreSQL 等数据库服务，再由 DBeaver 连接该服务。

## 文件

| 文件 | 用途 |
| --- | --- |
| [data/news.sqlite3](data/news.sqlite3) | 主数据库快照 |
| [data/news.csv](data/news.csv) | 同内容的 UTF-8 BOM CSV |
| [data/manifest.json](data/manifest.json) | 时间窗口、来源数量、覆盖范围、文件 SHA-256 |
| [data/progress.json](data/progress.json) | 当前进度、剩余数量、下次计划时间 |
| [data/source_reports/bbc.json](data/source_reports/bbc.json) | BBC 采集摘要 |
| [data/source_reports/sina.json](data/source_reports/sina.json) | 新浪财经采集摘要 |
| [data/source_reports/ft.json](data/source_reports/ft.json) | FT 采集摘要及健康状态 |
| [schema.sql](schema.sql) | 数据库结构；供检查或建立空库 |
| [queries.sql](queries.sql) | 常用 SQL |
| [scripts/validate_database.py](scripts/validate_database.py) | 只读验证工具，不需要第三方 Python 包 |
| [scripts/crawl_daily.py](scripts/crawl_daily.py) | 每日采集、去重、上限控制和安全试跑 |
| [crawler/](crawler/) | BBC、新浪与 FT 适配器和正文解析代码 |

仓库包含新闻数据、爬虫代码、测试和工作流。原始网页缓存、个人日志和连接凭据均未包含。Actions 使用仓库内置的临时令牌保存数据；FT 登录凭据只由本地采集程序读取。

## 数据表结构

主表 `news` 一行对应一篇新闻，全部字段为 `TEXT`；`article_id` 是主键，`url` 有唯一约束。完整定义见 [schema.sql](schema.sql)。

| 字段 | 含义 |
| --- | --- |
| `article_id` | 规范化 URL 的 SHA-256 前 32 位，作为稳定标识 |
| `source` | `BBC News`、`新浪财经` 或 `Financial Times` |
| `title` | 原语言新闻标题 |
| `content` | 解析到的文章正文（FT 使用获授权账户读取），保留原语言与段落 |
| `publish_time` | 原始发布时间，ISO 8601，带 `+08:00` 时区偏移 |
| `crawl_time` | 获取正文时间，ISO 8601，带 `+08:00` 时区偏移 |
| `url` | 原文链接；去除查询参数和片段后的规范化 URL |
| `language` | 语言标记，如 `en`、`zh-CN` |

辅助表 `collection_info(key, value)` 存储采集元数据。视图包括 `bbc_news`、`sina_news`、`ft_news`（按来源筛选）、`raw_news`（兼容同样的八字段）和 `source_summary`（每个来源的数量及最早、最晚发布时间）。

## 覆盖范围

- **BBC News：初始 23 篇。** 初始发现范围为 BBC World RSS 当时提供的 27 个条目，其中 4 个视频条目跳过。每日采集现覆盖头条、World、UK、Business、Politics、Technology、Science/Environment、Entertainment/Arts、Health 共 9 个官方 RSS，跨栏目去重。读取文章公开 JSON-LD 中的 `datePublished`，不把 RSS 更新时间当作原始发布时间。这不是 BBC 全站近两天的完整归档。
- **新浪财经：初始 158 篇。** 初始数据来自财经滚动列表（`pageid=153`、`lid=2516`）的 6 页结果。每日分页读取近期列表，并逐篇检查文章页面的原始发布时间；最多读取 100 页以限制请求。这不代表新浪所有频道的全部新闻。
- **Financial Times：** 从 World、Global Economy、Europe、US、Asia Pacific、Markets、Central Banks、Equities、Commodities、Currencies、Technology、Companies、Energy 分类 RSS 发现文章；只保存账户获准读取的可见正文，不把 RSS 摘要、付费墙或订阅广告当作新闻。
- FT 五天补录还会分页发现分类文章。单篇跳转到允许范围之外时拒绝跳转、记入跳过计数并继续下一篇；登录/订阅验证失败、403、429 仍停止采集。候选链接数量不等于最终可入库的正文数量。
- 五天自动补录成功入库后，个别文章解析失败或孤立网络错误会保留在报告中，但不会阻止继续下一批；来源被停止、访问限制、零新增或达到总上限时仍结束本轮补录。
- 窗口外、视频、直播、无可识别正文或可信发布时间、访问失败的条目不纳入快照。三个来源合并后，URL 唯一，标题与正文非空。

新闻正文和原始内容的权利归各自权利人。本仓库未对这些第三方内容授予开源许可；原文链接保留在每条记录中。

## 验证下载的文件

使用 Python 3.10 或更新版本，在仓库根目录运行：

```sh
python scripts/validate_database.py
```

也可以从其他目录指定仓库位置：

```sh
python scripts/validate_database.py --root <仓库目录>
```

验证器会检查 SQLite 完整性、manifest 中的文件 SHA-256 和计数、八字段结构、URL 和文章标识、北京时间时间窗、来源摘要、CSV 编码与逐字段一致性。所有检查只读，不会修改数据库；发生错误时以非零状态退出。

开发验证（Python 3.10+）：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/crawl_daily.py --probe --cloud-only
```

BBC／新浪使用 GitHub 定时任务；FT 使用本机定时任务上传批次，交由 GitHub 串行合并。遇到人工提交冲突时停止推送，不强制覆盖仓库。

FT 本机自动程序、Windows 任务和故障弹窗的操作方法见[本机自动采集说明](docs/ft-local-automatic.md)。日常采集与上传不需要 Codex 运行。

## 新闻 × 港股价格查询

团队网页：[打开查询系统](https://jonehandson11-spec.github.io/News-AI-quant-project/)（仓库拥有者启用 GitHub Pages 并成功运行发布工作流后开放）。可输入港股名称／代码和新闻发布时间，查看前 1／3／6 个月及后 1／3／12／24 小时、3 天、1 周的价格、实际取价时间和缺失／休市顺延状态。新闻候选从本仓库 `data/news.csv` 自动生成；候选仍需人工复核。

网页初始没有真实共享行情。成员可在自己的浏览器选择有权使用的价格 CSV；文件不会上传到服务器。若团队取得允许公开再分发的行情，可添加 `data/public_prices.csv` 供所有人直接查询。部署和数据格式见 [网页说明](web/README.md)，批量查询及统计见 [命令行说明](docs/news-price-query.md)。没有真实行情时，系统不会生成价格收益或预测指标。
