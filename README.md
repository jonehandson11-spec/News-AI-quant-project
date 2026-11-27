# 时政新闻与行情研究

[打开团队行情查询](https://news-price-query-en.onrender.com/) · [下载新闻 SQLite](data/news.sqlite3) · [下载新闻 CSV](data/news.csv)

## 新闻价格查询页面（入口）

使用英文界面查询港股和石油相关资产。选择标的或输入 `700`、`0883.HK`、`CL=F` 等代码。已登记的中文别名也可以输入。选日期和频率后点 **Look Up Price**，页面会直接显示最近已完成的价格、实际时间和数据来源，并自动加载 K 线和 GVS 指标。

- **Automatic** 根据实际行情返回最近一根已完成的价格，同一时刻优先更细周期。支持分钟、小时和日线，结果显示实际周期、交易所时间和来源。
- **Candlestick Chart** 默认采用查价返回的周期，可切换小时线、日线及日期范围。只有日收盘价的日期仍可查价，完整日 K 需要小时线。
- **GVS Market State and Quality** 显示日频行情指标、缺失原因和质量字段，支持导出 CSV。Q01 可接逐因子证据评级，Q02 采用约定的采集范围。缺数据时留空。M04 依赖买卖报价或盘口。
- **数据接入**使用 `GET /api/assets`、`GET /api/price` 和 `GET /api/bars`。`/api/price` 加 `market_state=1` 可取得 GVS 与质量字段。 [参数与接入格式](web/README.md) · [石油资产表](news_price_query/data/research_assets.csv)

[网页源码](web/index.html)由 Python 服务运行。团队入口使用 Render，部署配置跟随 `main`。旧 GitHub Pages 地址统一导向团队行情站。

<details>
<summary>本地运行</summary>

第一次运行时，在仓库目录安装依赖并准备行情库（Python 3.10 或更新版本）。

```sh
python -m pip install -r deploy/requirements.txt
python scripts/update_prices.py --universe news_price_query/data/universe_example_hk.csv --universe news_price_query/data/universe_oil_research.csv
```

已有价格库时可直接启动。Windows 双击 [START-WINDOWS.bat](START-WINDOWS.bat)，Mac/Linux 运行 `./start-local.sh`。浏览器打开 `http://127.0.0.1:8765/`。

本地行情库默认在 `~/.news_price_query/prices.sqlite3`。如需现场尝试从 Yahoo 补取未入库标的，运行 `python -m web.server --yahoo-ondemand --open-browser`。 [本地行情库说明](docs/price-store.md)

</details>

## 下载和在 DBeaver 中打开

新闻库包含 BBC News、新浪财经和 Financial Times 的文章。下载 [SQLite](data/news.sqlite3) 后，在 DBeaver 新建 SQLite 连接，选择文件并打开 `news` 表。也可用 [CSV](data/news.csv) 在 Excel 等工具查看。 [常用 SQL](queries.sql) · [数据进度](data/progress.json)

## 开发与维护

| 内容 | 入口 |
| --- | --- |
| 新闻字段、采集和验证 | [新闻库和采集说明](docs/news-collection.md) |
| FT 本机采集和恢复 | [FT 自动采集说明](docs/ft-local-automatic.md) |
| 行情与 GVS 接口 | [网页文档](web/README.md) |
| 行情更新和交易日历 | [本地价格库](docs/price-store.md) |
| 新闻事件研究脚本 | [命令行文档](docs/news-price-query.md) |
| Render 部署与价格快照 | [部署说明](deploy/README.md) |
| 旧查询页及旧版交接说明 | [Git 历史归档](https://github.com/jonehandson11-spec/News-AI-quant-project/tree/3be6990b84e7d348a43a78737f1366dfdb6b42d8) |
