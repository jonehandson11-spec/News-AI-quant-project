# 网页模式

> 现在默认用**本地价格库 + 交易日窗口**（不需要富途 OpenD，也不用导入 CSV）。先按 [docs/price-store.md](../docs/price-store.md) 运行一次 `python scripts/update_prices.py --universe ...`，再 `python -m web.server`，打开 <http://127.0.0.1:8765/>。旧的富途分钟模式：`python -m web.server --provider futu --mode minute`。下文是旧版说明。

本地网页地址是 <http://127.0.0.1:8765/>，需要先启动服务：Windows 在项目根目录双击 `START-WINDOWS.bat`，macOS／Linux 在项目根目录运行 `bash start-local.sh`。`index.html` 不能直接双击使用，因为查询需要 Python 服务连接富途 OpenD。首次安装、登录 OpenD 和团队共享步骤见[根目录说明](../README.md)。

`index.html`、`app.mjs`、`server.py` 和 `service.py` 组成**团队内部自动取价网页**。

浏览器从同源的 `/api/assets` 读取七个标的，从 `/api/news` 读取按标的和日期筛选的新闻，从 `/api/query` 获取服务端通过富途 OpenD 计算的价格窗口。新闻由服务端从团队仓库的 `data/news.csv` 自动刷新。价格不经过浏览器上传，也不存放在公开 Pages 构建产物里。

`static/` 保存原公开 GitHub Pages 的 CSV 版页面。`build_site.py` 继续从本地新闻 CSV 构建该静态版，以免现有公开网址误用内部接口。团队日常查询应打开内部服务地址；GitHub Pages 本身不能运行 Python 或连接本机 OpenD。

API 只供同源内部页面使用。团队共享时把服务放在内网或 VPN 中；如需公网入口，应先在前面加身份验证和访问控制。

## 访问码

可为每位使用者生成独立访问码，再通过 HTTPS 隧道共享网页。生成访问码并保存哈希文件和仅供管理员保管的明文清单：

```bash
python -m web.access generate --names 张三,李四 --hash-file /安全目录/access_codes.json --plain-file /安全目录/access_codes.txt
python -m web.server --access-codes /安全目录/access_codes.json   # 仍只监听 127.0.0.1
cloudflared tunnel --url http://127.0.0.1:8765         # 免费 quick tunnel，给出 https://….trycloudflare.com 链接
```

哈希文件旁会自动创建会话密钥和访问日志；也可用 `--session-secret`、`--access-log` 指定路径，或用环境变量 `NEWS_PRICE_ACCESS_CODES` 指定哈希文件。访问码明文清单应妥善保管，访问码、哈希文件、会话密钥和日志都不要放进仓库。开启后所有页面和 /api/* 须先登录（会话 cookie 30 天，HttpOnly、Secure、SameSite=Lax）；同一来源 15 分钟内输错 5 次会被限速。可用 `python -m web.access list --hash-file 路径` 查看标签；重新生成同名标签会替换该标签的访问码。

## 行情来源

内部网页使用富途 OpenD。旧命令行和公开静态版仍接受本地价格 CSV，格式为 `ticker,interval,timestamp,close[,basis]`；`interval` 为 `day` 或 `minute`，分钟时间需包含时区。例如：

```csv
ticker,interval,timestamp,close
1211.HK,day,2026-08-28,90.0
1211.HK,minute,2026-09-28T09:59:00+08:00,100.0
```

以上数字只展示文件格式，并非真实历史价格。
