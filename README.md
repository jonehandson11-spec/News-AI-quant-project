# BBC + 新浪财经新闻共享库

这是一个可下载、可用 Git 同步的 SQLite 新闻快照，共 **181 篇正文**：BBC News **23 篇**，新浪财经 **158 篇**。

采集窗口为北京时间 **2026-09-25 21:07:26 至 2026-09-27 21:07:26**，包含起止边界。按网页的原始发布时间筛选；这是一份固定的 48 小时快照，当前没有自动更新任务。

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
| [data/source_reports/bbc.json](data/source_reports/bbc.json) | BBC 采集摘要 |
| [data/source_reports/sina.json](data/source_reports/sina.json) | 新浪财经采集摘要 |
| [schema.sql](schema.sql) | 数据库结构；供检查或建立空库 |
| [queries.sql](queries.sql) | 常用 SQL |
| [scripts/validate_database.py](scripts/validate_database.py) | 只读验证工具，不需要第三方 Python 包 |

本仓库仅包含此次共享快照及使用、验证工具。爬虫运行环境、原始网页缓存、个人日志和连接凭据均未包含。

## 数据表结构

主表 `news` 一行对应一篇新闻，全部字段为 `TEXT`；`article_id` 是主键，`url` 有唯一约束。完整定义见 [schema.sql](schema.sql)。

| 字段 | 含义 |
| --- | --- |
| `article_id` | 规范化 URL 的 SHA-256 前 32 位，作为稳定标识 |
| `source` | `BBC News` 或 `新浪财经` |
| `title` | 原语言新闻标题 |
| `content` | 解析到的公开文章正文，保留原语言与段落 |
| `publish_time` | 原始发布时间，ISO 8601，带 `+08:00` 时区偏移 |
| `crawl_time` | 获取正文时间，ISO 8601，带 `+08:00` 时区偏移 |
| `url` | 原文链接；去除查询参数和片段后的规范化 URL |
| `language` | 语言标记，如 `en`、`zh-CN` |

辅助表 `collection_info(key, value)` 存储采集元数据。视图包括 `bbc_news`、`sina_news`（按来源筛选）、`raw_news`（兼容同样的八字段）和 `source_summary`（每个来源的数量及最早、最晚发布时间）。

## 覆盖范围

- **BBC News：23 篇。** 发现范围为 BBC World RSS 当时提供的 27 个条目，其中 4 个视频条目跳过。使用文章公开 JSON-LD 中的 `datePublished`，不把 RSS 的更新时间当作原始发布时间。这不是 BBC 全站近两天的完整归档。
- **新浪财经：158 篇。** 发现范围为财经滚动列表（`pageid=153`、`lid=2516`）的 6 页结果，并逐篇检查文章页面的原始发布时间。这不代表新浪所有频道的全部新闻。
- 窗口外、视频、直播、无可识别正文或可信发布时间、访问失败的条目不纳入快照。两个来源合并后，URL 唯一，标题与正文非空。

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
