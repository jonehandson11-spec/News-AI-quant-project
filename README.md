# 新闻 × 港股价格查询系统（独立代码包）

此包只包含**查询系统**的代码、测试和说明。源代码取自团队仓库提交 `732931a76b11161ec46f76d695c07f272a93cf77`。不包含新闻爬虫、采集工作流、FT 登录相关代码、新闻数据库快照或真实价格数据。

## 用途

将港股名称／代码与一条新闻的发布时间对齐，在有真实行情数据时查询新闻前 1／3／6 个月及后 1／3／12／24 小时、3 天、1 周的价格，并计算差价和涨跌幅。网页还会根据团队新闻库生成相关候选新闻供选择；Python 命令行支持扫描新闻和批量导出。结果保留实际取价时间、价格来源及缺失／休市顺延状态。

**当前功能边界：**网页一次只计算一条新闻；它展示最多 25 条直接提及的新闻候选。你提出的“输入 BYD 和时间范围，筛出该范围内全部相关新闻并作为一组逐条分析”尚未上线。团队仓库当前也没有可共用的真实港股历史价格源。没有价格数据时，系统不能计算真实差价。

## 直接使用

在线网页：[新闻 × 港股价格查询](https://jonehandson11-spec.github.io/News-AI-quant-project/)。

网页的新闻候选由团队仓库 `data/news.csv` 构建。输入 BYD／`1211.HK`，选一条候选新闻或输入香港时间；有权使用真实行情的成员可在浏览器选择一次价格 CSV，后续访问会自动读取。价格文件只保存在该浏览器，不会上传到网站或共享给其他成员。价格 CSV 是行情数据，不是新闻 CSV；格式见 [网页说明](web/README.md#行情来源)。

## 本机运行

需要 Python 3.10+。在解压后的包目录运行下列命令，从团队 GitHub 仓库获取**最新新闻 CSV**，再构建网页：

```sh
mkdir data
python -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/jonehandson11-spec/News-AI-quant-project/main/data/news.csv', 'data/news.csv')"
python web/build_site.py --news data/news.csv --output _site
python -m http.server 8765 --directory _site
```

浏览器打开 `http://127.0.0.1:8765/`。新闻 CSV 会在本机生成的 `data/` 目录中；这一步只下载新闻，**不提供股价**。

只扫描新闻、不需要价格文件：

```sh
python -m news_price_query scan --asset BYD --news data/news.csv --output byd_scan.json
```

取得真实且获准使用的行情 CSV 后，才可以批量计算价格：

```sh
python -m news_price_query batch --asset BYD --news data/news.csv --provider csv --prices /path/to/prices.csv --output byd_events.jsonl
```

计算逻辑与格式详见 [命令行说明](docs/news-price-query.md)。若有 Node.js，可运行网页计算测试：

```sh
python -m unittest discover -s price_query_tests -v
node --test web/core.test.mjs
```

## 包内文件

| 路径 | 用途 |
| --- | --- |
| `web/index.html`、`styles.css`、`app.mjs` | 网页界面与交互 |
| `web/core.mjs`、`local-prices.mjs` | 浏览器价格窗口计算与本机价格文件保存 |
| `web/build_site.py` | 从新闻 CSV 构建网页新闻索引 |
| `news_price_query/` | Python 资产匹配、新闻筛选、价格接口、批量查询和统计 |
| `price_query_tests/`、`web/core.test.mjs` | 查询系统测试 |
| `docs/news-price-query.md`、`web/README.md` | 详细使用说明 |

新闻正文和原始行情分别受其来源的使用条件约束。本包没有附带真实价格或把价格示例冒充历史行情；价格变化只是描述性观察，不证明新闻造成涨跌。
