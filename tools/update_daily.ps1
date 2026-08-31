# 彰化運行看板每日自動更新：回補最近 3 天（同日重跑=整日覆蓋，冪等）
# 由 Windows 工作排程「RunBoard-Update」每天 12:30 呼叫；漏跑會在下次開機補執行
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $tools 'update.log'
$from = (Get-Date).AddDays(-3).ToString('yyyyMMdd')
$to   = (Get-Date).AddDays(-1).ToString('yyyyMMdd')
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] 更新 $from ~ $to" | Add-Content -Encoding utf8 $log
try {
  $out = & python (Join-Path $tools 'fetch_runsheet.py') --from $from --to $to 2>&1
  $out | Out-String | Add-Content -Encoding utf8 $log
} catch {
  "錯誤: $_" | Add-Content -Encoding utf8 $log
}
# log 超過 500KB 砍半保留尾段
if ((Get-Item $log).Length -gt 500KB) {
  $tail = Get-Content $log -Tail 400
  Set-Content -Encoding utf8 $log $tail
}
