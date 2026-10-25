# 网页（新闻价格查询）

使用步骤见[根目录 README 的「新闻价格查询页面（入口）」](../README.md#新闻价格查询页面入口)：先用 `scripts/update_prices.py` 建本地价格库，再双击 `START-WINDOWS.bat` 或运行 `./start-local.sh`，打开 <http://127.0.0.1:8765/>。`index.html` 不能直接双击使用，查询需要 Python 服务。

## 文件与接口

- `index.html`、`app.mjs`、`core.mjs`、`styles.css`：页面；`server.py`、`service.py`：本机 Python 服务（默认 `--provider local --mode hourly`，监听 127.0.0.1:8765）。
- `/api/assets` 返回已登记标的（含研究环节、市场、币种、单位）；`/api/news` 按标的和日期筛选候选新闻（来自仓库 `data/news.csv`）；`/api/query` 从本地价格库按交易小时窗口计算结果（服务参数 `--mode daily` 保留交易日窗口）。
- 默认窗口为新闻前 6/3/1、新闻后 1/3/6 个交易小时，交易时段外、周末及假期不累计；小时线约覆盖最近两年，更早新闻显示缺数据。结果表显示起点、终点、原始涨跌和状态；「显示复权和超额」开关（记在 URL `?detail=1` 和 localStorage）加出复权与超额列。期货按固定合约计算，开关禁用。下载的 JSON/CSV 始终包含全部字段。
- `node --test web/` 运行前端测试。

API 只供同源本机页面使用；不要公开部署。如需团队共享，须先获 Ek 批准并加访问控制。

`static/` 是原公开 GitHub Pages 的旧版 CSV 页面（由 `build_site.py` 构建），保留不动，不是组员的查询入口。

## 访问码（组员公网链接）

开启后所有页面和 `/api/*` 须先登录：会话 cookie 30 天（HttpOnly、Secure、SameSite=Lax），同一来源 15 分钟内输错 5 次会被限速，登录记录写本地日志。可每人一个码，也可全组共用一个码（文件里只放一条）。

```bash
python -m web.access generate --names 全组共用 --hash-file /安全目录/access_codes.json --plain-file /安全目录/access_codes.txt
python -m web.server --access-codes /安全目录/access_codes.json   # 仍只监听 127.0.0.1
cloudflared tunnel --url http://127.0.0.1:8765         # 免费 quick tunnel，给出 https://….trycloudflare.com 链接
```

哈希文件旁会自动生成会话密钥和访问日志（也可用 `--session-secret`、`--access-log` 或环境变量 `NEWS_PRICE_ACCESS_CODES`）。服务每次登录都重新读取哈希文件，换码不用重启；删掉的码对应的会话立即失效。访问码、哈希文件、会话密钥和日志都不要放进仓库。

NYMEX 小时模式近似使用 Yahoo Globex 时段：交易日 D 的前一日 18:00 至 D 日 17:00 美东时间；日线仍按 14:30 结算时刻。香港小时线 12:30 时间戳按 13:00–13:30 短 bar 处理，15:30 bar 为收市 bar；该 bar 的常见零成交量不作为零成交标记。

旧版备选：`python -m web.server --provider futu --mode minute` 仍可用富途 OpenD 分钟取价（需 `futu-api` 和已登录的 OpenD）。
