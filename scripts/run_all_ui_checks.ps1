# The whole browser round of the project (about 12 minutes). Every check runs on a FRESH throwaway
# data folder and a spare port, never on Ivan's real data. Run it from anywhere:
#     powershell -NoProfile -File scripts\run_all_ui_checks.ps1
# Then read $env:TEMP\ui_summary.txt: one line per check (PASS / FAIL counts) and "ALL DONE" at the end.
# It starts and stops its own apps (ports 8792 to 8795) and never presses the map download button.
$root = Split-Path -Parent $PSScriptRoot
$py = "$root\.venv\Scripts\python.exe"
Set-Location $root
$summary = "$env:TEMP\ui_summary.txt"
"" | Set-Content $summary

function Fresh($name) {
  $f = "$env:TEMP\$name"
  if (Test-Path $f) { Remove-Item -Recurse -Force $f -ErrorAction SilentlyContinue }
  New-Item -ItemType Directory -Force $f | Out-Null
  return $f
}
function StartApp($folder, $seed, $port, $demo) {
  $env:JOBS_DATA_DIR = $folder
  & $py $seed 2>&1 | Out-Null
  if ($demo) { $a = Start-Process -FilePath $py -ArgumentList "scripts\ui_demo_server.py" -WorkingDirectory $root -PassThru -WindowStyle Hidden }
  else {
    $env:JOBS_SCHEDULER_ENABLED = "false"
    $a = Start-Process -FilePath $py -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port $port" -WorkingDirectory $root -PassThru -WindowStyle Hidden
    Remove-Item Env:JOBS_SCHEDULER_ENABLED
  }
  return $a
}
function StopApp($a) { Stop-Process -Id $a.Id -Force; Start-Sleep -Seconds 2 }
function Report($label, $out) {
  $p = ($out | Where-Object { $_ -match "^PASS" }).Count
  $f = ($out | Where-Object { $_ -match "^FAIL" }).Count
  $line = "$label : PASS $p  FAIL $f"
  $line | Add-Content $summary
  $out | Where-Object { $_ -match "^FAIL|SCRIPT ERROR|^FINDINGS|^  (light|dark)" } | Select-Object -First 15 | Add-Content $summary
}

# 1. accessibility scan: demo server (8795, pretend source) + places app (8794)
$f1 = Fresh "ui_final_refresh"; $demo = StartApp $f1 "scripts\seed_ui_inbox.py" 8795 $true
$f2 = Fresh "ui_final_places"; $pl = StartApp $f2 "scripts\seed_ui_demo.py" 8794 $false
Start-Sleep -Seconds 9
$a11y = Start-Process -FilePath node -ArgumentList "scripts\ui_check_a11y.js" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$env:TEMP\a11y_final.txt"

# 2. the street map check, both states, on port 8792 (while the scan runs); needs data\maps for "with"
foreach ($state in "with", "without") {
  if ($state -eq "with" -and -not (Test-Path "$root\data\maps")) { "street map check (with) : SKIPPED, no data\maps (press the download button on Mjesta first)" | Add-Content $summary; continue }
  $f = Fresh "ui_final_streets_$state"
  $env:JOBS_DATA_DIR = $f
  & $py scripts\seed_ui_streets.py 2>&1 | Out-Null
  if ($state -eq "with") { Copy-Item -Recurse "$root\data\maps" "$f\maps" }
  $env:JOBS_SCHEDULER_ENABLED = "false"
  $s = Start-Process -FilePath $py -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8792" -WorkingDirectory $root -PassThru -WindowStyle Hidden
  Remove-Item Env:JOBS_SCHEDULER_ENABLED
  Start-Sleep -Seconds 9
  $env:UI_STREETS = $state; $env:UI_BASE = "http://127.0.0.1:8792"
  $out = & node scripts\ui_check_streets.js 2>&1
  Remove-Item Env:UI_STREETS; Remove-Item Env:UI_BASE
  Report "street map check ($state)" $out
  StopApp $s
}

# 3. the other pages, on port 8793, each on fresh data
$f = Fresh "ui_final_inbox"; $a = StartApp $f "scripts\seed_ui_inbox.py" 8793 $false; Start-Sleep -Seconds 8
$env:UI_BASE = "http://127.0.0.1:8793"
Report "Inbox + job page" (& node scripts\ui_check_inbox.js 2>&1); StopApp $a
$f = Fresh "ui_final_admin"; $a = StartApp $f "scripts\seed_ui_inbox.py" 8793 $false; Start-Sleep -Seconds 8
Report "Izvori / Dodaj / Dnevnik" (& node scripts\ui_check_admin.js 2>&1); StopApp $a
$f = Fresh "ui_final_places2"; $a = StartApp $f "scripts\seed_ui_demo.py" 8793 $false; Start-Sleep -Seconds 8
Report "Places (old flows)" (& node scripts\ui_check_places.js 2>&1); StopApp $a
$f = Fresh "ui_final_apply"; $a = StartApp $f "scripts\seed_ui_apply.py" 8793 $false; Start-Sleep -Seconds 8
Report "Prijave (apply pages)" (& node scripts\ui_check_apply.js 2>&1); StopApp $a
$f = Fresh "ui_final_townonly"; $a = StartApp $f "scripts\seed_ui_townonly.py" 8793 $false; Start-Sleep -Seconds 8
Report "Town-only ads (4.10)" (& node scripts\ui_check_townonly.js 2>&1); StopApp $a
Remove-Item Env:UI_BASE

# 4. wait for the accessibility scan
$a11y.WaitForExit(600000) | Out-Null
$out = Get-Content "$env:TEMP\a11y_final.txt"
Report "accessibility scan (all pages)" $out
($out | Select-Object -Last 4) | Add-Content $summary
StopApp $demo; StopApp $pl

# 5. refresh, motion and the Gmail page on the demo server, fresh each (the demo server has a PRETEND
#    mail server and an in-memory password store: nothing real is contacted or saved)
foreach ($script in "scripts\ui_check_refresh.js", "scripts\ui_check_motion.js", "scripts\ui_check_gmail.js") {
  $f = Fresh ("ui_final_" + [IO.Path]::GetFileNameWithoutExtension($script))
  $d = StartApp $f "scripts\seed_ui_inbox.py" 8795 $true; Start-Sleep -Seconds 9
  Report $script (& node $script 2>&1); StopApp $d
}
"ALL DONE" | Add-Content $summary
