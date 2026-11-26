# 云端部署（Render 免费版）

- 网页：Render 免费 Web Service，`render.yaml` + `deploy/cloud_start.py`。不需要信用卡，用 GitHub 登录。
- 价格：`deploy/publish-prices.workflow.yml`（需复制到 `.github/workflows/publish-prices.yml`，机器人账号没有 workflow 权限） 每个工作日 18:00 UTC 用 yfinance 更新价格库，压缩后发到 Release `prices-latest`；网页每次启动时下载。
- 访问：云端网页无需访问码，拿到链接即可使用。若平台上仍留着 `NEWS_PRICE_ACCESS_CODES_JSON` 或 `NEWS_PRICE_SESSION_SECRET`，启动脚本会忽略，不必删。本地启动（`python -m web.server --access-codes`）不受影响。

注意：
- 免费版 15 分钟没人访问会休眠，再打开要等约 1 分钟。
- 仓库公开，Release 里的价格库（Yahoo 数据）任何人都能下载；云端网页本身也不再设访问码。
- 构建命令需安装 `deploy/requirements.txt`（含 yfinance）。若仪表盘里仍是 `pip install tzdata`，请改成 `pip install -r deploy/requirements.txt` 再 Manual Deploy。启动脚本在缺少 yfinance 时也会尝试安装。
- 价格库没有的标的会现场从 Yahoo 拉取并写入运行时数据库；免费版磁盘是临时的，重启后缓存会丢，可再拉。
- 本地（OpenD / 本地价格库）用法不变；本机默认不现场拉取，需显式加 `--yahoo-ondemand`。
