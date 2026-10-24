# 网页（新闻价格查询）

使用步骤见[根目录 README 的「新闻价格查询页面（入口）」](../README.md#新闻价格查询页面入口)：先用 `scripts/update_prices.py` 建本地价格库，再双击 `START-WINDOWS.bat` 或运行 `./start-local.sh`，打开 <http://127.0.0.1:8765/>。`index.html` 不能直接双击使用，查询需要 Python 服务。

## 文件与接口

- `index.html`、`app.mjs`、`core.mjs`、`styles.css`：页面；`server.py`、`service.py`：本机 Python 服务（默认 `--provider local --mode daily`，监听 127.0.0.1:8765）。
- `/api/assets` 返回已登记标的（含研究环节、市场、币种、单位）；`/api/news` 按标的和日期筛选候选新闻（来自仓库 `data/news.csv`）；`/api/query` 从本地价格库按交易日窗口计算结果。
- 结果表默认显示窗口、起点、终点、涨跌幅（原始）、状态；「显示复权和超额」开关（记在 URL `?detail=1` 和 localStorage）加出复权与超额列。期货按固定合约计算，开关禁用。下载的 JSON/CSV 始终包含全部字段。
- `node --test web/` 运行前端测试。

API 只供同源本机页面使用；不要公开部署。如需团队共享，须先获 Ek 批准并加访问控制。

`static/` 是原公开 GitHub Pages 的旧版 CSV 页面（由 `build_site.py` 构建），保留不动，不是组员的查询入口。

旧版备选：`python -m web.server --provider futu --mode minute` 仍可用富途 OpenD 分钟取价（需 `futu-api` 和已登录的 OpenD）。
