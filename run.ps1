$ErrorActionPreference = 'Stop'
& py -3 -X utf8 (Join-Path $PSScriptRoot 'server.py') @args
