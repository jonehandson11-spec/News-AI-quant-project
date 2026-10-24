# 本地价格库（日线）

查询网页和命令行默认从本地 SQLite 价格库取日线：不用导入 CSV，也不用登录富途 OpenD。富途和 CSV 仍可作备选。

- 代码：`news_price_query/price_store.py`（库和 `LocalDbPriceProvider`）、`news_price_query/price_update.py`（增量更新）、`scripts/update_prices.py`（命令）
- 库文件位置：默认 `~/.news_price_query/prices.sqlite3`；可用环境变量 `NEWS_PRICE_DB` 或 `--db` 改。库文件**不进仓库**（已加 .gitignore）
- 数据源：yfinance 日线，仅限内部研究，不对外发布或再分发
- 目前只实现港股（XHKG 日历）。表里有 `market`、`currency` 字段，日历按 market 选取，以后可加其他市场

## 安装与更新

```sh
pip install yfinance            # 只有更新脚本需要；查询本身只用标准库
python scripts/update_prices.py --universe news_price_query/data/universe_example_hk.csv
python scripts/update_prices.py --universe /path/to/your_pool.csv --universe news_price_query/data/universe_example_hk.csv
```

- 股票池 CSV 需要有 `ticker`、`yfinance代码` 或 `代码`（如 00883）列；`中文名`、`英文名`、`常用别名`（用 ; 分隔）会被读入，用于网页别名和新闻匹配。仓库只放了只含代码的示例池；团队股票池文件不要提交。
- 盈富基金 2800.HK 和恒生指数 ^HSI 总是一起更新（作为对照）。
- 增量：已有的代码只从最后一个已存交易日往前 `--overlap-days`（默认 10）个交易日开始重拉，用来接住数据修正。若重叠区间里 复权价/收盘价 的比例变了（新的分红或拆股会改变整段复权历史），就自动整段重拉该代码。
- 非交易日的行（yfinance 的假日填充行）按**交易日历**丢弃，不按成交量为 0 删除：半日市成交量常为 0，但价格是真的。
- 当天（香港日期）的行不入库，因为可能未定稿；下次运行再写入。
- 每次运行记一条 `update_runs`（run_id、开始/结束时间、状态、行数），每行价格记 `source`、`fetched_at`、`run_id`；查询结果带 `data_version`。
- 输出会列出每个代码：已存行数、日历交易日数、缺失交易日（日历有、数据源没有）、丢弃的非交易日行数。

### 每天自动更新

脚本可直接被定时任务调用（退出码 0 = 全部成功，1 = 部分失败）。建议在香港收盘后、次日开盘前运行，例如每天 HKT 20:00 之后或 Paris 时间早上：

```sh
cd /path/to/News-AI-quant-project && python scripts/update_prices.py --universe /path/to/pool.csv
```

## 查询

```sh
python -m web.server            # 默认：本地价格库 + 交易日窗口，仅 127.0.0.1:8765
python -m web.server --provider futu --mode minute   # 旧版：富途 OpenD + 分钟窗口
python -m news_price_query query --asset 0883.HK --time 2026-09-28T11:17:42+08:00 --provider local --mode daily
```

结果每个窗口给出：原始涨跌幅（`return_pct`，收盘价仅拆股调整）、复权涨跌幅（`adj_return_pct`，含分红）、相对 2800.HK 和 ^HSI 的超额收益（原始和复权两套，`excess_return_pct` / `adj_excess_return_pct`，简单差值）、状态和来源。

状态与标记：
- `status`：`on_date`、`deferred`（目标日无行情，往后顺延，最多 5 个交易日）、`deferred_back`、`pending`（未到期）、`missing`。缺失从不当作 0。
- `flag`：`missing_data` = 目标交易日该股没有行情行（停牌或数据缺失，免费数据分不清，如实标缺数据）；`zero_volume` = 非半日市的交易日成交量为 0（零成交，或数据源没给成交量）。指数不打 `zero_volume`。

网页只在本机运行（默认监听 127.0.0.1）。是否在内网或公网共享须 Ek 批准，并先加访问控制。
