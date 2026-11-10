# 在「以管理员身份运行」的 PowerShell 里执行。
# 把办公室网络设成专用网络，放行本子网的 TCP 47800 / UDP 47801，并在登录后启动通讯员。
# 这些命令没有在真实 Windows 上跑过，第一次请浩年或博洋在自己电脑上核对一遍。

$ErrorActionPreference = "Stop"

Get-NetConnectionProfile | Format-Table InterfaceAlias, NetworkCategory
# 把下面的 "WLAN" 换成上面看到的办公室网卡名字
# Set-NetConnectionProfile -InterfaceAlias "WLAN" -NetworkCategory Private

New-NetFirewallRule -DisplayName "Xiaolingtong gateway (TCP 47800)" -Direction Inbound -Protocol TCP -LocalPort 47800 -Action Allow -Profile Private -RemoteAddress LocalSubnet
New-NetFirewallRule -DisplayName "Xiaolingtong discovery (UDP 47801)" -Direction Inbound -Protocol UDP -LocalPort 47801 -Action Allow -Profile Private -RemoteAddress LocalSubnet

$py = (py -3 -c "import sys; print(sys.executable)")
$pyw = $py -replace 'python\.exe$','pythonw.exe'
$xlt = Join-Path $env:USERPROFILE ".xlt\venv\Scripts\xlt.exe"
if (-not (Test-Path $xlt)) { $xlt = $pyw }
$arg = if ($xlt -like "*pythonw.exe") { "-m xlt serve" } else { "serve" }
$act = New-ScheduledTaskAction -Execute $xlt -Argument $arg -WorkingDirectory "$env:USERPROFILE\.xlt"
$trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "Xiaolingtong Gateway" -Action $act -Trigger $trg -Settings $set -Description "小灵通通讯员（登录后自启）"
Start-ScheduledTask -TaskName "Xiaolingtong Gateway"
