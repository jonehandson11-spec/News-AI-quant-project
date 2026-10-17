# 网页模式

本地网页地址是 <http://127.0.0.1:8765/>，需要先启动服务：Windows 在项目根目录双击 `START-WINDOWS.bat`，macOS／Linux 在项目根目录运行 `bash start-local.sh`。`index.html` 不能直接双击使用，因为查询需要 Python 服务连接富途 OpenD。首次安装、登录 OpenD 和团队共享步骤见[根目录说明](../README.md)。

`index.html`、`app.mjs`、`server.py` 和 `service.py` 组成**团队内部自动取价网页**。

浏览器从同源的 `/api/assets` 读取七个标的，从 `/api/news` 读取按标的和日期筛选的新闻，从 `/api/query` 获取服务端通过富途 OpenD 计算的价格窗口。新闻由服务端从团队仓库的 `data/news.csv` 自动刷新。价格不经过浏览器上传，也不存放在公开 Pages 构建产物里。

`static/` 保存原公开 GitHub Pages 的 CSV 版页面。`build_site.py` 继续从本地新闻 CSV 构建该静态版，以免现有公开网址误用内部接口。团队日常查询应打开内部服务地址；GitHub Pages 本身不能运行 Python 或连接本机 OpenD。

API 只供同源内部页面使用。团队共享时把服务放在内网或 VPN 中；如需公网入口，应先在前面加身份验证和访问控制。

## 行情来源

内部网页使用富途 OpenD。旧命令行和公开静态版仍接受本地价格 CSV，格式为 `ticker,interval,timestamp,close[,basis]`；`interval` 为 `day` 或 `minute`，分钟时间需包含时区。例如：

```csv
ticker,interval,timestamp,close
1211.HK,day,2026-08-28,90.0
1211.HK,minute,2026-09-28T09:59:00+08:00,100.0
```

以上数字只展示文件格式，并非真实历史价格。
