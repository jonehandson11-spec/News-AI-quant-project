# 云端部署（Render 免费版）

- 网页：Render 免费 Web Service，`render.yaml` + `deploy/cloud_start.py`。不需要信用卡，用 GitHub 登录。
- 价格：`deploy/publish-prices.workflow.yml`（需复制到 `.github/workflows/publish-prices.yml`，机器人账号没有 workflow 权限） 每个工作日 18:00 UTC 用 yfinance 更新价格库，压缩后发到 Release `prices-latest`；网页每次启动时下载。
- 访问码：Render 环境变量 `NEWS_PRICE_ACCESS_CODES_JSON` 填访问码**哈希文件**内容（不是明文访问码）；`NEWS_PRICE_SESSION_SECRET` 填 64 位十六进制，休眠重启后不用重新登录。没有访问码哈希时拒绝启动。

注意：
- 免费版 15 分钟没人访问会休眠，再打开要等约 1 分钟。
- 仓库公开，Release 里的价格库（Yahoo 数据）任何人都能下载；访问码只保护网页。
- 本地（OpenD / 本地价格库）用法不变。
