# Proves the project works from nothing, the way the GitHub Actions run will see it (plan 6.5).
# Copies ONLY the files git would include (tracked + new, minus everything in .gitignore: so no
# data, no .venv, no database, no CVs) into an empty folder, makes a fresh virtual environment,
# installs with the same command as .github/workflows/ci.yml, then runs the linter and the tests.
#     powershell -NoProfile -File scripts\check_clean_clone.ps1
# Prints "CLEAN CLONE OK" at the end, or the first failing step. About 6 minutes. Never touches
# your real data folder.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $env:TEMP "clean_clone_check"
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
New-Item -ItemType Directory -Force $target | Out-Null
Set-Location $root

$files = git ls-files --cached --others --exclude-standard
$count = 0
foreach ($f in $files) {
  if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { continue }  # deleted but not yet committed
  $dest = Join-Path $target $f
  New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
  Copy-Item -LiteralPath $f -Destination $dest
  $count++
}
"copied $count files to $target"

Set-Location $target
$bad = Get-ChildItem -Recurse -Include "*.db", "*.pdf", ".env" -ErrorAction SilentlyContinue
if ($bad) { throw "private-looking files were copied: $($bad.FullName -join ', ')" }

python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw "could not create the virtual environment" }
$py = Join-Path $target ".venv\Scripts\python.exe"
$env:JOBS_DATA_DIR = Join-Path $target "throwaway-data"  # tests make their own, this is just a belt
& $py -m pip install -q -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "install failed" }
& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "ruff found problems" }
& $py -m pytest -q --cov --cov-report=term
if ($LASTEXITCODE -ne 0) { throw "tests failed" }
"CLEAN CLONE OK"
