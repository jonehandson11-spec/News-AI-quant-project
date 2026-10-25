# BBC + 新浪财经 + Financial Times 新闻共享库

## 自动采集

每天北京时间 **20:00** 开始，UTC 为 **12:00**，每轮只新增最近 48 小时发布的文章。三个来源合计达到配置中的总量上限后停止新增；已有数据保留。网站没有足够文章时不会生成填充数据。

- **BBC、新浪财经：** GitHub Actions 云端采集，不依赖个人电脑。
- **Financial Times：** 在已授权的本机环境采集，每轮最多 100 篇；本机自动任务通过 GitHub 连接器上传文章批次，云端验证后合并。电脑须联网、保持开机且 Codex 正在运行；关闭或休眠时无法本地采集，恢复运行后可补一次最近计划周期。
- 两条云端写入工作流使用同一个并发锁。FT 批次进入独立的接收分支，云端取得最新主库后追加、去重，保留 BBC、新浪和历史 FT 数据。不会用旧本机数据库覆盖云端主库。
- GitHub 调度及本机任务可能有延迟，不能保证整点立即启动。当前数量与剩余条数见 [data/progress.json](data/progress.json)。

运行入口：[BBC／新浪每日采集](https://github.com/jonehandson11-spec/News-AI-quant-project/actions/workflows/crawl.yml)、[本地 FT 批次导入](https://github.com/jonehandson11-spec/News-AI-quant-project/actions/workflows/import-ft.yml)。云端每日任务的 **probe** 只验证 BBC／新浪的临时数据库；**crawl** 立即正式采集这两个来源。

配置见 [crawl_config.json](crawl_config.json)。FT 本地程序见 [scripts/ft_sync.py](scripts/ft_sync.py)，批次验证与合并见 [scripts/ft_local.py](scripts/ft_local.py)。本地状态和凭据目录必须在 Git 仓库外；上传仅限准备程序列出的文章批次，不上传 Cookie 或本机日志。只有在主库报告中核验批次回执后才确认完成；上传失败或导入被取消时保留原批次，用同一批次重试。

需要手动补录最近五天的 FT 时，在同一 `prepare` 命令中明确加上 `--lookback-hours 120`。此模式可在当天自动采集已执行后运行，但仍先恢复未确认批次，每批最多 100 篇、三个来源合计不超过配置中的总量上限。每批须核验主库回执并执行 `acknowledge` 后才能继续。默认每日采集窗口仍是 48 小时；补录不会更改定时计划。补录只接受符合真实发布时间窗口且获准读取的正文，发现范围或访问权限不足时不能保证达到配置中的目标数量。

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

## 新闻价格查询页面（入口）

**组员直接用：** 打开 <https://bookmarks-administrator-administrator-marks.trycloudflare.com>，输入组长单独发的访问码即可使用，什么都不用装。链接只在组长电脑开着时可用；组长重开后链接会变，以本 README 为准。

输入股票（或期货）和新闻发布时间查询价格；不用导入 CSV，也不用登录富途 OpenD。覆盖港股、美股、加股、沙特股票和 Brent/WTI/RBOB/ULSD 期货（石油研究首期标的见 `news_price_query/data/research_assets.csv`）。

**第一次用：先建本地价格库**（需要 Python 3.10+；以后每天重跑同一命令即可增量更新）：

```sh
python3 -m pip install yfinance
python3 scripts/update_prices.py --universe news_price_query/data/universe_example_hk.csv --universe news_price_query/data/universe_oil_research.csv
```

Windows 把 `python3` 换成 `py -3`。价格库默认在 `~/.news_price_query/prices.sqlite3`，不进仓库。同一命令会顺带存 1 小时线：第一次补约两年的小时线，之后每天增量更新；日线照常更新。

**启动（两步）：**

1. Windows 双击 `START-WINDOWS.bat`；Mac/Linux 在仓库目录运行 `./start-local.sh`（窗口保持打开）。
2. 浏览器会自动打开 `http://127.0.0.1:8765/`；没打开就手动访问这个地址。

**怎么查：**选标的 → 选择候选新闻或输入发布时间（香港时间）→ 查询。默认窗口为新闻前 6、3、1 个交易小时，以及新闻后 1、3、6 个交易小时。交易小时只计算交易时段，午休、收盘后、周末和假期不计；小时线约覆盖最近两年，更早新闻显示「缺数据」。结果展示起止时间、价格、币种/单位、原始涨跌和状态；「显示复权和超额」可查看复权及同市场对照。期货固定合约规则见本地价格库说明。缺数据不按零处理，未到期显示「未到期」。可下载 JSON/CSV。命令行默认使用小时窗口；需交易日窗口时加 `--mode daily`，例如 `python3 -m news_price_query query --asset 0883.HK --time 2026-09-28T11:17:00+08:00 --mode daily --provider local --format table`。

Yahoo 香港小时线的 `12:30` 时间戳对应午休后 13:00–13:30 的短 bar；`15:30` bar 截止收市，成交量常为零的情况属于数据源记时/成交量伪影，不据此标记零成交。NYMEX 小时窗口按 Yahoo Globex 近似时段计算：交易日 D 的前一日 18:00 至 D 日 17:00 美东时间；日线仍按 14:30 结算时刻。

价格数据来自 yfinance，**仅供组内研究**；网页默认只在本机 127.0.0.1 运行，不要公开部署或把 8765 端口开放到公网。价格与新闻同时变化不代表因果关系。详细说明见 [本地价格库](docs/price-store.md)（更新、交易日历、期货规则）、[命令行](docs/news-price-query.md)、[网页说明](web/README.md)。

旧版备选：代码仍保留富途 OpenD 取价（`--provider futu --mode minute`），不再作为组员使用方式。
