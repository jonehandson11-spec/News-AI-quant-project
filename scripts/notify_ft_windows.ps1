param([Parameter(Mandatory=$true)][ValidatePattern('^[a-z_]{1,64}$')][string]$Reason)
Add-Type -AssemblyName PresentationFramework
$title = 'FT 需要处理'
$detail = switch ($Reason) {
    'rate_limited' { 'FT 返回 429 限流。程序已暂停请求并保存进度，将按冷却时间和下一计划周期处理；无需据此更新登录凭据。' }
    { $_ -in @('auth_expired','auth_required','login_or_subscription_required') } { 'FT 登录或订阅验证未通过。请先在浏览器确认正文可读，再更新本机 D:\news_crawlers\private\ft_cookie.txt。不要把 Cookie 发到聊天或公开仓库。' }
    'access_denied' { 'FT 返回 403 拒绝访问。不能据此认定 Cookie 过期；请查看本地状态记录。' }
    'recovered' { $title = 'FT 已恢复'; 'FT 批次已成功合并，三个来源仍在原 GitHub 仓库 data/news.sqlite3 的 news 表。' }
    'target_reached' { $title = '新闻库已达标'; '三来源合计已达到 3000 条，程序按设定停止采集。' }
    default { '本机采集或 GitHub 导入需要检查。故障代码：' + $Reason }
}
$clock = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd HH:mm:ss')
$text = "北京时间：$clock`n`n$detail`n`n旧数据仍保留。状态：D:\news_crawlers\ft-sync\autorun-result.json`n`n点击「是」打开 GitHub 运行记录，点击「否」关闭。"
$answer = [System.Windows.MessageBox]::Show($text, $title, 'YesNo', 'Information')
if ($answer -eq 'Yes') { Start-Process 'https://github.com/jonehandson11-spec/News-AI-quant-project/actions' }
