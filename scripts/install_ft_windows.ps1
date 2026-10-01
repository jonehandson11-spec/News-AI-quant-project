param([switch]$InspectOnly)
$ErrorActionPreference = 'Stop'
$taskName = 'FT Local Collect and GitHub Sync'
if ($InspectOnly) {
    Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State, Actions, Triggers
    Get-ScheduledTaskInfo -TaskName $taskName
    exit
}
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'run_ft_windows.ps1'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
$start = [DateTimeOffset]::UtcNow
$start = $start.AddMinutes(60 - $start.Minute).AddSeconds(-$start.Second).AddMilliseconds(-$start.Millisecond)
$boundary = $start.ToString('yyyy-MM-ddTHH:mm:00Z')
$scriptXml = [Security.SecurityElement]::Escape($script)
$rootXml = [Security.SecurityElement]::Escape($root)
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Collect FT once per Beijing 20:00 slot and upload validated batches to the existing GitHub news database. Hourly checks recover pending uploads; no AI runtime is required.</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger><Repetition><Interval>PT1H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$boundary</StartBoundary><Enabled>true</Enabled></TimeTrigger>
    <LogonTrigger><Enabled>true</Enabled><UserId>$sid</UserId></LogonTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled><Hidden>false</Hidden><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec><Command>C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe</Command><Arguments>-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File &quot;$scriptXml&quot;</Arguments><WorkingDirectory>$rootXml</WorkingDirectory></Exec></Actions>
</Task>
"@
Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName $taskName | Select-Object NextRunTime, LastTaskResult
