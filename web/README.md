# 港股与石油行情查询

使用步骤见[根目录 README 的「新闻价格查询页面（入口）」](../README.md#新闻价格查询页面入口)：先用 `scripts/update_prices.py` 建本地价格库，再双击 `START-WINDOWS.bat` 或运行 `./start-local.sh`，打开 <http://127.0.0.1:8765/>。`index.html` 不能直接双击使用，查询需要 Python 服务。

页面默认查价格。选择港股或石油研究标的，或输入代码（例如 `700`、`0700.HK`、`CL=F`），再选日线或小时线。留空日期时查最近已完成的记录。所选日期按交易所当地日历解释。若当天没有完成的价格，界面显示此前 14 天内最近的已完成价格和实际日期；没有记录则显示缺数据。价格来自本地 SQLite 行情库，启用 `--yahoo-ondemand` 时可尝试从 Yahoo 补取缺少的标的。中文名称取决于资产登记和本地股票池别名。

日线查价可直接使用日收盘价或期货结算价。K 线仍使用小时 OHLC 和由小时线聚合的日 K，只有日收盘价的日期无法画出完整蜡烛。石油入口复用 19 项研究资产，`BZ=F` 是 NYMEX Brent 代理，登记为无免费来源的 ICE 低硫柴油仍显示为不可查。

展开市场因子可按同一标的和日期查看日线市场状态，并保存因子值、缺失原因、基准及计算口径。市场状态沿用 `pq-market-state-daily-v1` 的估算方法，小时线查价也使用这套日线因子。M04 流动性压力依赖买卖价差或盘口数据，目前留空；期货尚无默认基准，M05 和 M06 留空。数据为当前库中可查的历史记录，尚无逐时点行情修订快照。

行情页只提供查价、K 线和市场因子。原新闻及事件窗口仍可由已有脚本通过 API 调用。

## 文件与接口

- `index.html`、`app.mjs`、`chart.mjs`、`core.mjs`、`styles.css`：页面。`vendor/lightweight-charts/` 为 Apache-2.0 授权的 TradingView Lightweight Charts 5.3.0（无 CDN 依赖，见目录内 LICENSE / NOTICE）。`server.py`、`service.py` 为本机 Python 服务，默认监听 127.0.0.1:8765。
- `GET /api/assets` 返回已登记标的、市场、币种、单位和 `asset_id`（未登记 ID 的股票保留其标准 `ticker`）。
- `GET /api/bars` 按 `symbol`、`interval`（1h / 1d）、`start` 和 `end` 返回 OHLC。页面 K 线可切换周期、选择日期、拖动或缩放。
- `/api/price?symbol=0700.HK&interval=1d&date=2026-09-28` 查询价格、数据来源和实际取价时间。`date` 可省略，加 `market_state=1` 返回因子。可将首次响应的 `cutoff_at` 作为后续请求的 `as_of`（带时区），以复用相同截点。该查询独立于新闻索引。
- 原 `GET /api/news` 使用仓库 `data/news.csv`（可由 `--news-csv` 指定）；`POST /api/query` 仍支持新闻前后 1、3、6 个交易小时的研究查询。行情页不调用这两个接口。
- `node --test web/core.test.mjs` 运行前端计算测试。

## GVS 质量数据接入

`market_state.quality` 与 `market_state.fields` 分开。`fields.data_coverage` 是行情诊断，统计最近 274 个预期交易日内有有效价格的比例。`quality` 对应 [GVS 定义 §4](https://docs.google.com/document/d/1JJFNRm5jv00nEGm1yuAOuvNyUyOwFl0P/edit)。

| 字段 | 接入约定 | 默认值 |
| --- | --- | --- |
| Q01 `evidence_reliability` | 证据评估结果。可通过 `by_variable` 按因子名提供独立评分。同一记录只有逐因子评分时保留整体空值。 | `null`，`ESTIMATOR_NOT_READY` |
| Q02 `data_coverage` | 预先登记的来源和时间范围下的覆盖率。`scope_id` 指向范围、计数规则、分母和核验记录。`0` 表示已核实该范围完全无数据。 | `null`，`REFERENCE_MISSING` |

接入时，在 `QueryService(..., gvs_quality_source=load_quality)` 或 `build_service(args, gvs_quality_source=load_quality)` 传入只读数据适配器。适配器以关键字接收 `asset`、带时区的 `as_of` 和 `profile_id`，用 `asset.asset_id`（缺失时用 `asset.ticker`）匹配已经生成的质量记录。它返回 `evidence_reliability` 和 `data_coverage` 两个条目。每个条目可包含 `value`、`missing_reason`、`method_version` 和 `source_ref`。Q01 的 `by_variable` 中每个因子使用同一结构；Q02 有值时需同时提供 `scope_id`。数值必须有限且在 0–1 之间，有值时必须有 `method_version`。用 `source_ref` 记录适合出现在查询结果里的数据记录 ID 或引用。

如果 Q01 来自大模型评级，`method_version` 对应模型和提示词版本，`source_ref` 指向带原文证据的评分记录。Q02 的范围和覆盖核验由采集记录提供。当前没有接入质量数据源，两个值默认留空。

因子 CSV 保留行情覆盖率列，另列 `q01_evidence_reliability`、`q02_data_coverage` 和各自的缺失原因。逐因子 Q01、方法版本和范围可从 `/api/price` 的 JSON 结构读取。普通价格查询无需质量数据。

API 只供同源本机页面使用；不要公开部署。如需团队共享，须先获 Ek 批准并加访问控制。

旧 GitHub Pages 地址通过 `build_site.py` 生成跳转，统一指向 [团队行情站](https://news-price-query-en.onrender.com/)。旧版 CSV 页面保存在 [Git 历史](https://github.com/jonehandson11-spec/News-AI-quant-project/tree/3be6990b84e7d348a43a78737f1366dfdb6b42d8/web/static)。

## 访问码（可选，仅本地或隧道）

线上 Render 站点已不再启用访问码，拿到链接即可使用。若要在本机或隧道上开启访问码，开启后所有页面和 `/api/*` 须先登录：会话 cookie 30 天（HttpOnly、Secure、SameSite=Lax），同一来源 15 分钟内输错 5 次会被限速，登录记录写本地日志。可每人一个码，也可全组共用一个码（文件里只放一条）。

```bash
python -m web.access generate --names 全组共用 --hash-file /安全目录/access_codes.json --plain-file /安全目录/access_codes.txt
python -m web.server --access-codes /安全目录/access_codes.json   # 仍只监听 127.0.0.1
cloudflared tunnel --url http://127.0.0.1:8765         # 免费 quick tunnel，给出 https://….trycloudflare.com 链接
```

哈希文件旁会自动生成会话密钥和访问日志（也可用 `--session-secret`、`--access-log` 或环境变量 `NEWS_PRICE_ACCESS_CODES`）。服务每次登录都重新读取哈希文件，换码不用重启；删掉的码对应的会话立即失效。访问码、哈希文件、会话密钥和日志都不要放进仓库。

NYMEX 小时模式近似使用 Yahoo Globex 时段：交易日 D 的前一日 18:00 至 D 日 17:00 美东时间；日线仍按 14:30 结算时刻。香港小时线 12:30 时间戳按 13:00–13:30 短 bar 处理，15:30 bar 为收市 bar；该 bar 的常见零成交量不作为零成交标记。

旧版备选：`python -m web.server --provider futu --mode minute` 仍可用富途 OpenD 分钟取价（需 `futu-api` 和已登录的 OpenD）。
