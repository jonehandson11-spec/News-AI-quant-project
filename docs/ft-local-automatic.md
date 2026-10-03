# 本机 FT 自动采集和上传

FT 由这台 Windows 电脑上的 Python 程序采集，独立 SSH 密钥把批次上传到原仓库 `jonehandson11-spec/News-AI-quant-project`。GitHub Actions 将批次去重后追加到 `main` 的 `data/news.sqlite3`，与 BBC、新浪共用 `news` 表和 3000 条总上限。无需 Codex 参与每次运行。

Windows 任务名为 `FT Local Collect and GitHub Sync`。每小时检查一次并在登录后补查，程序按北京时间最近的 20:00 周期每天最多采集一次，默认最近 48 小时。电脑需开机、登录并联网；关闭 Codex 不影响任务。关机、睡眠或注销期间无法采集，恢复后补最近一轮。任务不会强制唤醒电脑。

本机位置：

- 程序：`D:\news_crawlers\github-verified\scripts\ft_autorun.py`
- 一键入口：`scripts\run_ft_windows.ps1`
- 私密登录文件：`D:\news_crawlers\private\ft_cookie.txt`
- 上传密钥：`D:\news_crawlers\private\ft_github_ed25519`（只在本机，仓库设置只保存公钥）
- 运行状态：`D:\news_crawlers\ft-sync\autorun-result.json`
- 安全摘要日志：`D:\news_crawlers\ft-sync\autorun-log.jsonl`

在 PowerShell 中运行 `& 'D:\news_crawlers\github-verified\scripts\run_ft_windows.ps1'` 可立即检查当前周期。加 `-Backfill` 可手动补最近 120 小时，加 `-ResumeOnly` 只恢复待上传或待确认批次，加 `-Check` 仅检查配置。补录发现范围为配置的 RSS 与有限栏目页，不保证 FT 全站完整归档，也不保证凑满 3000 条。

默认请求间隔 10 秒，遇到 429 立即停止，并至少冷却一小时；如 FT 的 Retry-After 要求更久，则等待更久。冷却期间仍可上传已保存批次。程序不绕过登录、订阅、robots 或访问限制。浏览器能读某篇正文，并不能保证 RSS 与程序请求也获准。自动化不会消除 FT 的服务端限流。

新故障通过本机弹窗提示，同一原因不反复弹；恢复和达到总量上限各提示一次。账号或订阅验证失败时，需要本人确认权限并更新本机 Cookie 文件。不要把 Cookie 或私钥发到聊天、提交到 GitHub 或写进日志。

上传只发布 `incoming/ft-batch.json`，不会从本机覆盖主数据库。未确认入库的批次会保留，后续优先恢复，不重复请求 FT。进程锁在程序结束或被终止时由操作系统释放。收到 main 分支的匹配批次回执，才算完成。

GitHub 导入记录分别显示 **Import**（合并和保存是否成功）与 **Submitted batch collection**（这批文章的采集状态）。已成功保存的批次如果部分文章采集失败，会保留具体原因并显示 warning，不再把成功导入标为失败。真实合并、校验、推送失败仍会标红；采集端登录或订阅问题、403、429 的停止机制和本机通知继续生效。报告仅保留严格验证的公开 FT 原文链接，方便检查具体出错页面，不包含登录参数或私密文件信息。

安装或重新注册本机任务：运行 `scripts\install_ft_windows.ps1`。程序使用原仓库唯一的 `codex/ft-inbox` 收件分支，不创建额外仓库。
