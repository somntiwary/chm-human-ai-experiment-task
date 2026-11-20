[CmdletBinding()]
param([switch]$Detached)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$demoHttp = $null
try {
    # Read the same .env/Compose overrides the app uses, without printing secrets.
    $demoConfigJson = & docker compose -f compose.yml -f compose.ollama.yml config --format json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the demo configuration. Start Docker Desktop and retry.' }
    $demoConfig = $demoConfigJson | ConvertFrom-Json
    $demoModel = $demoConfig.services.backend.environment.OLLAMA_MODEL
    $demoUrl = [Uri]$demoConfig.services.backend.environment.OLLAMA_BASE_URL

    if ($demoUrl.Host -eq 'host.docker.internal' -and $demoUrl.Scheme -eq 'http') {
        # Docker Desktop can reach this loopback-only Windows server. No global
        # environment changes, LAN listener, model pull, or administrator access.
        Add-Type -AssemblyName System.Net.Http
        $demoHandler = New-Object System.Net.Http.HttpClientHandler
        $demoHandler.UseProxy = $false
        $demoHttp = New-Object System.Net.Http.HttpClient($demoHandler)
        $demoHttp.Timeout = [TimeSpan]::FromSeconds(2)
        $demoTagsUrl = "http://127.0.0.1:$($demoUrl.Port)/api/tags"
        function Get-DemoOllamaTags {
            try { return ($demoHttp.GetStringAsync($demoTagsUrl).GetAwaiter().GetResult() | ConvertFrom-Json) }
            catch { return $null }
        }

        $demoTags = Get-DemoOllamaTags
        if ($null -eq $demoTags) {
            $demoCommand = Get-Command ollama -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            $demoExe = if ($demoCommand) { $demoCommand.Source } else { Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
            if (-not (Test-Path -LiteralPath $demoExe)) {
                throw 'Windows Ollama was not found. Install/open Ollama with your existing model, then retry.'
            }
            Write-Host 'Starting installed Windows Ollama (no model download)...'
            $demoPreviousHost = $env:OLLAMA_HOST
            try {
                $env:OLLAMA_HOST = "127.0.0.1:$($demoUrl.Port)"
                $demoServer = Start-Process -FilePath $demoExe -ArgumentList 'serve' -WindowStyle Hidden -PassThru
            }
            finally { $env:OLLAMA_HOST = $demoPreviousHost }
            for ($demoAttempt = 0; $demoAttempt -lt 30; $demoAttempt++) {
                $demoTags = Get-DemoOllamaTags
                if ($null -ne $demoTags) { break }
                Start-Sleep -Milliseconds 500
            }
            if ($null -eq $demoTags) {
                throw 'Windows Ollama did not become reachable. Open Ollama and check port 11434, then retry.'
            }
        }
        if (-not ($demoTags.models | Where-Object { $_.name -eq $demoModel })) {
            throw "The configured model '$demoModel' is not installed in Windows Ollama. Check 'ollama list' and OLLAMA_MODEL. No model was downloaded."
        }
        Write-Host "Using installed Windows model: $demoModel"
    }
    else {
        Write-Host 'Using the configured Ollama server; ensure it is running before startup.'
    }

    $demoComposeArgs = @('compose', '-f', 'compose.yml', '-f', 'compose.ollama.yml', 'up', '--build')
    if ($Detached) { $demoComposeArgs += '-d' }
    & docker @demoComposeArgs
    if ($LASTEXITCODE -ne 0) { throw 'Demo startup failed. Read the service error above, correct it, and retry.' }
}
finally {
    if ($null -ne $demoHttp) { $demoHttp.Dispose() }
    Pop-Location
}
