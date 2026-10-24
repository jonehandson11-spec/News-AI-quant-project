# 新闻价格查询：命令行

`news_price_query/` 是查询程序，与[网页](../web/README.md)使用同一套交易日窗口和本地价格库。Python 3.10+，查询核心只用标准库；先按[本地价格库](price-store.md)建库。

## 单条查询

```sh
python3 -m news_price_query query --asset 0883.HK --time 2026-09-28T11:17:00+08:00 --mode daily --provider local --format table
```

- `--format table` 每个窗口打印起点、终点（日期 + 价格 + 币种/单位）、原始和复权涨跌幅，以及对照标的的起止价和超额；默认 `json` 输出全部字段（含 `start_*`/`end_*`、`price_unit`、期货连续合约参考等）。
- `--time` 推荐带时区偏移；无偏移时须加 `--timezone Asia/Hong_Kong`。`--as-of` 固定观察截止时间；`--before-days`、`--after-days` 改窗口（默认 5,21,63,126 和 1,3,5 个交易日）。
- 状态：`on_date`、`deferred`（目标日无行情，顺延）、`pending`（未到期）、`missing`、`expired`（期货合约已到期）。缺数据从不当作 0。

## 新闻扫描

```sh
python3 -m news_price_query scan --asset 0883.HK --news data/news.csv --output scan.json
```

直接提及只是候选，需要人工核验相关性和重复报道。价格变化只是描述性观察，不代表因果关系。

旧版备选：`--provider csv --prices <文件>` 和 `--provider futu`（富途 OpenD、分钟窗口）以及 `batch`/`study` 命令仍在代码里，不再作为组员使用方式。
