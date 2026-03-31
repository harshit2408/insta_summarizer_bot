# scrape.ps1 – convenience wrapper for the Instagram scraper
# Usage:  .\scrape.ps1 https://www.instagram.com/reel/ABC123/
# Usage:  .\scrape.ps1 --unit
# Usage:  .\scrape.ps1 --metadata-only https://www.instagram.com/reel/ABC123/

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$python = "C:\Users\send2\miniconda3\envs\myenv\python.exe"

& $python "$PSScriptRoot\test_scraper.py" @args
