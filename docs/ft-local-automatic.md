# 本机 FT 自动采集和上传

FT 由这台 Windows 电脑上的 Python 程序采集，独立 SSH 密钥把批次上传到原仓库 `jonehandson11-spec/News-AI-quant-project`。GitHub Actions 将批次去重后追加到 `main` 的 `data/news.sqlite3`，与 BBC、新浪共用 `news` 表和 `crawl_config.json` 设定的总上限（初始 3000 条，明确批准的历史补录可提高上限）。无需 Codex 参与每次运行。

2026-10-09 接入用户提供的 `ftnews_ltc.zip` 版本：正文提取改用原脚本的 `trafilatura.extract`，设置为 `favor_precision=True`、关闭评论和表格提取，保留至少 600 字符的要求。依赖固定为 `trafilatura==2.3.1`；部署代码后在原 Python 环境执行 `python -m pip install -r requirements.txt`。现有 13 个 RSS 栏目与附件相同；页面改版后不再因缺少旧正文 CSS 标记就直接丢弃，而由提取器识别可见正文。显式订阅拦截仍停止；隐藏节点、脚本及数据属性中的内容不作为正文，文章原始发布时间仍由页面元数据验证。

该版本接入已有的一次采集、批次保存、上传及恢复流程；附件的独立每小时无限循环和独立 `ft_news.db` 不同时运行。继续使用下述北京时间 20:00 计划和合并库总上限。附件 Cookie、截图、数据库和清理脚本不提交到仓库；附件的清理脚本也不作用于共享数据库。更换正文提取器不能保证登录长期有效，也不能解除 FT 的 403 或 429。

Windows 任务名为 `FT Local Collect and GitHub Sync`。每小时检查一次并在登录后补查，程序按北京时间最近的 20:00 周期每天最多采集一次，默认最近 48 小时。电脑需开机、登录并联网；关闭 Codex 不影响任务。关机、睡眠或注销期间无法采集，恢复后补最近一轮。任务不会强制唤醒电脑。

本机位置：

- 程序：`D:\news_crawlers\github-verified\scripts\ft_autorun.py`
- 一键入口：`scripts\run_ft_windows.ps1`
- 私密登录文件：`D:\news_crawlers\private\ft_cookie.txt`
- 上传密钥：`D:\news_crawlers\private\ft_github_ed25519`（只在本机，仓库设置只保存公钥）
- 运行状态：`D:\news_crawlers\ft-sync\autorun-result.json`
- 安全摘要日志：`D:\news_crawlers\ft-sync\autorun-log.jsonl`

在 PowerShell 中运行 `& 'D:\news_crawlers\github-verified\scripts\run_ft_windows.ps1'` 可立即检查当前周期。加 `-Backfill` 可手动补最近 120 小时，加 `-ResumeOnly` 只恢复待上传或待确认批次，加 `-Check` 仅检查配置。补录发现范围为配置的 RSS 与有限栏目页，不保证 FT 全站完整归档，也不保证达到配置中的目标数量。

默认请求间隔 10 秒，遇到 429 立即停止，并至少冷却一小时；如 FT 的 Retry-After 要求更久，则等待更久。冷却期间仍可上传已保存批次。程序不绕过登录、订阅、robots 或访问限制。浏览器能读某篇正文，并不能保证 RSS 与程序请求也获准。自动化不会消除 FT 的服务端限流。

新故障通过本机弹窗提示，同一原因不反复弹；恢复和达到总量上限各提示一次。账号或订阅验证失败时，需要本人确认权限并更新本机 Cookie 文件。不要把 Cookie 或私钥发到聊天、提交到 GitHub 或写进日志。

采集前最多检查两篇此前成功保存的 FT 文章，确认当前会话能读到完整正文后再请求栏目。这些对照文章不会重复入库；它们可读也不代表所有文章都在订阅范围内。本轮已读到其他完整正文时，单篇 HTML 订阅提示记为 `article_access_unavailable`，跳过该篇并保留 partial 状态；连续两篇或累计三篇受限即停止。HTTP 401、登录重定向、403、429 和 robots 拒绝仍立即停止，不会重试同篇来强刷权限。

认证失败后，相同本机凭据会进入等待更新状态，后续检查不会反复请求 FT 或上传空批次。更新 `ft_cookie.txt` 后，程序识别实际凭据变化，下次每小时检查或手动运行一键入口时可在同一天重新预检，不必等到下一天。仅修改空格、触碰文件或在浏览器登录不算更新凭据。私有状态 `D:\news_crawlers\ft-sync\access-state.json` 只保存凭据摘要和失败原因，不上传。所有入口（包括手动 `prepare --force` 和 120 小时回填）都遵守认证等待与 429 冷却；已保存批次仍优先完成导入。

Windows 上，确认完整正文可读后，程序会用当前 Windows 用户的 DPAPI 加密保存 FT 正常响应更新的会话到本机 `D:\news_crawlers\private\ft_session.dpapi`，下次运行复用。缓存损坏或不可解密时回退到原凭据文件；手动更换原凭据后旧缓存失效。程序不修改原 `ft_cookie.txt`，不读取浏览器存储，不把会话缓存提交到 GitHub。服务端要求重新登录或账号订阅变化时仍需本人处理，程序不能保证登录永久有效。

上传只发布 `incoming/ft-batch.json`，不会从本机覆盖主数据库。未确认入库的批次会保留，后续优先恢复，不重复请求 FT。进程锁在程序结束或被终止时由操作系统释放。收到 main 分支的匹配批次回执，才算完成。

GitHub 导入记录分别显示 **Import**（合并和保存是否成功）与 **Submitted batch collection**（这批文章的采集状态）。已成功保存的批次如果部分文章采集失败，会保留具体原因并显示 warning，不再把成功导入标为失败。真实合并、校验、推送失败仍会标红；采集端登录或订阅问题、403、429 的停止机制和本机通知继续生效。报告仅保留严格验证的公开 FT 原文链接，方便检查具体出错页面，不包含登录参数或私密文件信息。

安装或重新注册本机任务：运行 `scripts\install_ft_windows.ps1`。程序使用原仓库唯一的 `codex/ft-inbox` 收件分支，不创建额外仓库。

用户提供的旧 `ft_news.db` 可以单独历史补录：`scripts/ft_archive.py prepare` 只读提取文章，按附件原程序的北京时间解释无时区时间，排除过短正文、订阅宣传文本及重复记录。准备出的 `incoming/ft-archive.json` 由 `codex/ft-archive-inbox` 分支触发 `Merge supplied FT archive`，与每日采集使用同一并发锁，在云端最新数据库上追加；不从本机覆盖共享数据库。

历史补录保留所有旧文章，将上限提高至合并后的实际总量（若已有更高上限则保留），同时更新 SQLite、CSV、统计及哈希。补录来源、拒收数量、原始数据库摘要和文章编号记录在 `data/import_reports/`。原始 48 小时种子窗口保持不变，累计覆盖区间按补录记录扩展；附件中的发布时间来自 RSS，未重新请求文章页面核验。历史补录不更新 FT 的实时采集健康状态，也不表示登录或 403 问题已恢复。重复提交同一批次不会重复入库。
