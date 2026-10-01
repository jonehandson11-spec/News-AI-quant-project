# 港股新闻与价格查询：命令行

仓库中的 `news_price_query/` 是可复现的 Python 查询程序，与 [网页](../web/README.md)使用同一组时间窗口。支持输入港股名称／代码和新闻发布时间，也支持读取 `data/news.csv` 后批量筛选直接提及资产的文章。Python 3.10+，核心计算只用标准库。

## 查询顺序

1. `assets.py` 把资产名称转成港股代码；已登记比亚迪、腾讯、阿里巴巴、中芯国际、汇丰，其他港股可输入代码。
2. `news.py` 读取团队八列新闻 CSV，保留文章 ID、来源、时间和链接；直接提及只是候选，需要人工核验、去重。
3. `prices.py` 从本地 CSV 或本机富途 OpenD 获取日线与分钟线。
4. `query.py` 以已完成的发布前分钟 K 线为基准，匹配九个窗口并记录真实取价时间。
5. `study.py` 汇总成功、缺失、顺延和未到期样本以及描述性收益；样本足够后才考虑训练预测模型。

## 新闻扫描

```sh
python -m news_price_query scan --asset BYD --news data/news.csv --output byd_scan.json
```

## 单条价格查询

```sh
python -m news_price_query query --asset BYD --time 2026-09-28T10:00:00+08:00 --provider csv --prices /path/to/prices.csv
```

`--time` 推荐含 `+08:00`。无偏移时间必须同时指定 `--timezone Asia/Hong_Kong`。可加 `--as-of` 固定观察截止时间，可加 `--benchmark 2800.HK` 比较盈富基金。富途接口用 `--provider futu`，但需先安装 `futu-api`、启动 OpenD 并具备历史行情权限；此连接尚未使用真实账户验证。

价格 CSV 格式见[网页使用说明](../web/README.md#行情来源)。输出包含基准价格与时间、目标与实际取价时间、价格来源、未复权收益和状态。休市顺延标为 `deferred`，未来窗口为 `pending`，缺数据为 `missing`。价格口径混用、重复 K 线和异常价格会被拒绝。

## 批量与统计

```sh
python -m news_price_query batch --asset BYD --news data/news.csv --provider csv --prices /path/to/prices.csv --output byd_events.jsonl
python -m news_price_query study --input byd_events.jsonl
```

批量可加 `--format csv` 输出一篇新闻九行的表。输入价格必须是**真实且获准使用的数据**。仓库没有内置真实分钟行情；缺失时不会生成收益或模型指标。当前仅报告描述性价格变化，不声称因果关系或交易绩效。
