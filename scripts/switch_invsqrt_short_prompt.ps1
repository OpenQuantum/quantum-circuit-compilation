param(
    [Parameter(Mandatory)][ValidateSet('Switch','Verify','Rollback')][string]$Action,
    [Parameter(Mandatory)][string]$StateDirectory,
    [Parameter(Mandatory)][string]$WorkspaceRepository,
    [Parameter(Mandatory)][string]$ModelCredentialFile
)

$ErrorActionPreference = 'Stop'
if (!(Get-Command Start-Process).Parameters.ContainsKey('Environment')) {
    throw 'Run this isolated launcher in PowerShell 7.4 or later.'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$state = (Resolve-Path -LiteralPath $StateDirectory).Path
$workspace = (Resolve-Path -LiteralPath $WorkspaceRepository).Path
$previousRepository = (Resolve-Path -LiteralPath (Join-Path $state '../..')).Path
$oldResearch = Join-Path (Split-Path $previousRepository) 'quantum-research-workspace'
$officialRepo = Join-Path (Split-Path $previousRepository) 'qcc-invsqrt-optimization'
$next = Join-Path $state 'short-prompt'
$oldWorld = 'qcc-invsqrt-graph-demo-world'
$newWorld = 'qcc-invsqrt-short-prompt-world'
$oldVolume = 'qcc-invsqrt-graph-demo-world-state-20261117'
$newVolume = 'qcc-invsqrt-short-prompt-state'
$worldImage = 'quantum-world-invsqrt:local'
$worldPython = '/opt/quantum-world/bin/python'
$runtimePython = Join-Path $state 'runtime-venv/Scripts/python.exe'
$node = (Get-Command node -ErrorAction Stop).Source

function Invoke-Docker([string[]]$CommandArgs) {
    & docker @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw "Docker operation failed: $($CommandArgs[0])" }
}

function World-Running([string]$Container) {
    $status = docker inspect $Container --format '{{.State.Running}}' 2>$null
    return ($LASTEXITCODE -eq 0 -and $status -eq 'true')
}

function Assert-IdleWorld([string]$Container) {
    $query = @'
import sqlite3
c=sqlite3.connect('file:/state/world/world.sqlite3?mode=ro',uri=True)
n=c.execute("select count(*) from objects where kind in ('experiment','optimization') and coalesce(json_extract(body,'$.status'),'') not in ('succeeded','failed','cancelled','interrupted','completed','improved','no_improvement')").fetchone()[0]
print(n)
'@
    $active = Invoke-Docker @('exec',$Container,$worldPython,'-c',$query)
    if ($active -ne '0') { throw 'There is active or unknown World work; wait before switching.' }
}

function Assert-IdleBff([string]$Directory) {
    $db = Join-Path $Directory 'bff/runs.sqlite3'
    if (!(Test-Path -LiteralPath $db)) { return }
    $query = @'
import sqlite3, sys
from pathlib import Path
c=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True)
n=c.execute("select count(*) from documents where collection='runs' and coalesce(json_extract(body,'$.status'),'') not in ('succeeded','failed','cancelled','interrupted','completed')").fetchone()[0]
print(n)
'@
    $active = & $runtimePython -c $query $db
    if ($LASTEXITCODE -ne 0 -or $active -ne '0') { throw 'There is active or unknown Console work; wait before switching.' }
}

function Stop-Host([string]$Directory,[string]$Name,[string]$Marker) {
    $pidFile = Join-Path $Directory "$Name.pid"
    if (!(Test-Path -LiteralPath $pidFile)) { return }
    $rootId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $processes = Get-CimInstance Win32_Process
    $process = $processes | Where-Object ProcessId -eq $rootId
    if (!$process) { return }
    if (!$process.CommandLine -or !$process.CommandLine.Replace('/','\').Contains($Marker.Replace('/','\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Current $Name process does not match its isolated launcher."
    }
    function Stop-Tree($Entry) {
        $processes | Where-Object ParentProcessId -eq $Entry.ProcessId | ForEach-Object { Stop-Tree $_ }
        Stop-Process -Id $Entry.ProcessId -ErrorAction SilentlyContinue
    }
    Stop-Tree $process
}

function Launch-Host([string]$Directory,[string]$Name,[string]$Exe,[string[]]$LaunchArgs,[hashtable]$ChildEnv) {
    $out = Join-Path $Directory 'logs'
    New-Item -ItemType Directory -Path $out -Force | Out-Null
    $privateEnv = @{
        DEEPSEEK_API_KEY=$null; HUB_CLIENT_TOKEN=$null; QUANTUM_OPERATOR_TOKEN=$null;
        QUANTUM_RESEARCH_TOKEN=$null; QUANTUM_RESEARCH_RESULTS_TOKEN=$null;
        QCC_AGENT_TOKEN=$null; QCC_WORLD_RESULT_TOKEN=$null; QCC_WORLD_RESULT_WORLD_ID=$null
    }
    foreach ($key in $ChildEnv.Keys) { $privateEnv[$key] = $ChildEnv[$key] }
    $arguments = foreach ($arg in $LaunchArgs) {
        if ($arg.Contains('"')) { throw 'Unexpected quote in a launcher argument.' }
        if ($arg -match '\s') { '"' + $arg + '"' } else { $arg }
    }
    $process = Start-Process -FilePath $Exe -ArgumentList $arguments -WorkingDirectory $Directory -Environment $privateEnv `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $out "$Name.switch.stdout.log") `
        -RedirectStandardError (Join-Path $out "$Name.switch.stderr.log") -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $Directory "$Name.pid")
}

function Read-WorldCredential([string]$Name) {
    $row = $legacyEnvironment | Where-Object { $_.StartsWith("$Name=") }
    if ($row.Count -ne 1) { throw "Existing World is missing $Name." }
    return $row.Substring($Name.Length+1)
}

function Read-ModelCredential {
    $matches = [regex]::Matches((Get-Content -LiteralPath $ModelCredentialFile -Raw),
        '(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,160}(?![A-Za-z0-9_-])')
    if ($matches.Count -ne 1) { throw 'Expected one DeepSeek credential in the authorized file.' }
    return $matches[0].Value
}

function Launch-Research([string]$Directory,[string]$ResearchRepo) {
    Launch-Host $Directory 'research' $node @((Join-Path $ResearchRepo 'src/server.mjs')) @{
        QCC_AGENT_TOKEN=$env:QUANTUM_RESEARCH_TOKEN; QCC_DATA_DIR=(Join-Path $Directory 'research');
        QCC_HOST='127.0.0.1'; QCC_PORT='8787'; QCC_WORLD_RESULT_TOKEN=$env:QCC_WORLD_RESULT_TOKEN;
        QCC_WORLD_RESULT_WORLD_ID=$env:QCC_WORLD_RESULT_WORLD_ID
    }
}

function Launch-Runtime([string]$Directory,[string]$RuntimeConfig) {
    Launch-Host $Directory 'runtime' $runtimePython @('-m','agent_runtime','serve','--config',$RuntimeConfig) @{
        HUB_CLIENT_TOKEN=$env:HUB_CLIENT_TOKEN; DEEPSEEK_API_KEY=(Read-ModelCredential);
        NO_PROXY='127.0.0.1,localhost,api.deepseek.com'
    }
}

function Launch-RuntimeAndBff([string]$Directory,[string]$ConsoleRepo,[string]$RuntimeConfig,[string]$BffPython) {
    Launch-Runtime $Directory $RuntimeConfig
    Launch-Host $Directory 'bff' $BffPython @((Join-Path $ConsoleRepo 'scripts/console.py'),'--state-dir',$Directory,'serve-bff') @{
        HUB_CLIENT_TOKEN=$env:HUB_CLIENT_TOKEN
    }
}

function Add-Relay([string]$Container,[string]$File) {
    $content = Get-Content -LiteralPath $File -Raw
    Invoke-Docker @('exec','-d',$Container,$worldPython,'-c',$content)
}

function Wait-Http([string]$Url) {
    $expires = [DateTimeOffset]::UtcNow.AddSeconds(30)
    do {
        try {
            if ((Invoke-WebRequest -Uri $Url -TimeoutSec 2).StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $expires)
    throw "HTTP service not ready at $Url"
}

if ((Split-Path $previousRepository -Leaf) -ne 'qcc-invsqrt-graph-demo') {
    throw 'Use the isolated qcc-invsqrt-graph-demo state directory as the switch source.'
}

$legacyEnvironment = Invoke-Docker @('inspect',$oldWorld,'--format','{{json .Config.Env}}') | ConvertFrom-Json
$env:HUB_CLIENT_TOKEN = Read-WorldCredential 'HUB_CLIENT_TOKEN'
$env:QUANTUM_OPERATOR_TOKEN = Read-WorldCredential 'QUANTUM_OPERATOR_TOKEN'
$env:QUANTUM_RESEARCH_TOKEN = Read-WorldCredential 'QUANTUM_RESEARCH_TOKEN'

if ($Action -eq 'Switch') {
    if ((Test-Path -LiteralPath $next) -or (Invoke-Docker @('volume','ls','--format','{{.Name}}')) -contains $newVolume) {
        throw 'A short-prompt state already exists. Inspect it or use Rollback.'
    }
    if (!(World-Running $oldWorld)) { throw 'The previous isolated World must be running for the idle check.' }
    Read-ModelCredential | Out-Null
    Assert-IdleWorld $oldWorld
    Assert-IdleBff $state
    New-Item -ItemType Directory -Path $next | Out-Null
    foreach ($name in 'accounts.json','agents.json','worlds.json','console.json','login-code','operator-token','runtime.toml','world.short-prompt.toml') {
        Copy-Item -LiteralPath (Join-Path $state $name) -Destination (Join-Path $next $name)
    }
    $configuredRuntime = Join-Path $next 'runtime.short-prompt.toml'
    & $runtimePython (Join-Path $repo 'scripts/prepare_invsqrt_runtime.py') --base-config (Join-Path $next 'runtime.toml') --output $configuredRuntime
    if ($LASTEXITCODE -ne 0) { throw 'Runtime profile verification failed.' }

    $env:UV_PROJECT_ENVIRONMENT = Join-Path $next 'bff-venv'
    & uv sync --offline --locked --python 3.13 --project (Join-Path $repo 'quantum-bff') --quiet
    if ($LASTEXITCODE -ne 0) { throw 'BFF environment preparation failed.' }
    $bffPython = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts/python.exe'
    Remove-Item Env:UV_PROJECT_ENVIRONMENT
    $worldConfig = Join-Path $next 'world.short-prompt.toml'
    $worldSource = Join-Path $repo 'quantum-world/src'
    $sourceMount = "type=bind,src=$worldSource,dst=/source,readonly"
    $configMount = "type=bind,src=$worldConfig,dst=/config/world.toml,readonly"
    $resultToken = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
    $env:QUANTUM_RESEARCH_RESULTS_TOKEN = $resultToken
    $env:QCC_WORLD_RESULT_TOKEN = $resultToken
    $env:QCC_WORLD_RESULT_WORLD_ID = 'quantum-world-invsqrt-local'
    Invoke-Docker @('run','--rm','--pull=never','--network','none','--env','PYTHONPATH=/source',
        '--env','QUANTUM_RESEARCH_TOKEN','--env','QUANTUM_RESEARCH_RESULTS_TOKEN',
        '--mount',$sourceMount,'--mount',$configMount,$worldImage,$worldPython,'-c',
        'from quantum_world.config import Settings; Settings.load("/config/world.toml")')

    $researchData = Join-Path $next 'research'
    New-Item -ItemType Directory -Path $researchData | Out-Null
    & $node --input-type=module -e 'import {DatabaseSync,backup} from "node:sqlite"; const source=new DatabaseSync(process.argv[1],{readOnly:true}); await backup(source,process.argv[2]); source.close();' `
        (Join-Path $state 'research/workspace.sqlite') (Join-Path $researchData 'workspace.sqlite')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to copy the Workspace snapshot.' }

    Assert-IdleBff $state
    Assert-IdleWorld $oldWorld
    Invoke-Docker @('stop','--timeout','12',$oldWorld)
    Invoke-Docker @('volume','create',$newVolume)
    Invoke-Docker @('run','--rm','--pull=never','--network','none',
        '--mount',"type=volume,src=$oldVolume,dst=/old,readonly",
        '--mount',"type=volume,src=$newVolume,dst=/new",'busybox:1.37.0','sh','-c',
        'test -s /old/world/world.sqlite3 && cp -a /old/. /new/ && test -s /new/world/world.sqlite3')
    Stop-Host $state 'bff' $state
    Stop-Host $state 'runtime' $state
    Stop-Host $state 'research' $oldResearch

    $env:QUANTUM_DOCKER_SELF = $newWorld
    Launch-Research $next $workspace
    Wait-Http 'http://127.0.0.1:8787/api/graph'
    $metric = Join-Path $officialRepo 'official/metric'
    $receipt = Join-Path $officialRepo 'runs/invsqrt-full.json'
    Invoke-Docker @('run','-d','--pull=never','--name',$newWorld,'--network','quantum-smoke-1110-134127',
        '-p','127.0.0.1:8331:8321','--env','HUB_CLIENT_TOKEN','--env','QUANTUM_OPERATOR_TOKEN',
        '--env','QUANTUM_RESEARCH_TOKEN','--env','QUANTUM_RESEARCH_RESULTS_TOKEN','--env','QUANTUM_DOCKER_SELF',
        '--env','PYTHONPATH=/source','--mount',$sourceMount,'--mount',$configMount,
        '--mount',"type=volume,src=$newVolume,dst=/state,volume-nocopy",
        '--mount','type=volume,src=quantum-world-invsqrt-local-official,dst=/project/official/data,readonly,volume-nocopy',
        '--mount',"type=bind,src=$metric,dst=/project/official/metric,readonly",
        '--mount',"type=bind,src=$receipt,dst=/project/runs/invsqrt-full.json,readonly",
        '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
        $worldImage,'quantum-world','--config','/config/world.toml','serve','--operator-http','--http-host','0.0.0.0','--http-port','8321')
    Add-Relay $newWorld (Join-Path $PSScriptRoot 'invsqrt_loopback_relay.py')
    Launch-RuntimeAndBff $next $repo $configuredRuntime $bffPython
} elseif ($Action -eq 'Verify') {
    if (!(World-Running $newWorld)) { throw 'The isolated short-prompt World is not running.' }
    Read-ModelCredential | Out-Null
    Assert-IdleBff $next
    Assert-IdleWorld $newWorld
    Stop-Host $next 'runtime' $next
    try {
        Invoke-Docker @('restart','--timeout','12',$newWorld)
        Add-Relay $newWorld (Join-Path $PSScriptRoot 'invsqrt_loopback_relay.py')
        & $runtimePython (Join-Path $PSScriptRoot 'check_invsqrt_short_world.py') `
            --baseline-full-id 'exp-f3ad5ee95baf455c9bb687e2bafa8dc4' `
            --baseline-quick-id 'exp-b9aa3929e68f47919bfc6e39da323c2e'
        if ($LASTEXITCODE -ne 0) { throw 'The read-only World SDK preflight failed.' }
    } finally {
        Launch-Runtime $next (Join-Path $next 'runtime.short-prompt.toml')
    }
} else {
    Assert-IdleBff $next
    if (World-Running $newWorld) {
        Assert-IdleWorld $newWorld
        Invoke-Docker @('stop','--timeout','12',$newWorld)
    }
    Stop-Host $next 'bff' $next
    Stop-Host $next 'runtime' $next
    Stop-Host $next 'research' $workspace
    if (!(World-Running $oldWorld)) {
        Invoke-Docker @('start',$oldWorld)
        Add-Relay $oldWorld (Join-Path $state 'research-relay.py')
    }
    if (!(Test-NetConnection -ComputerName '127.0.0.1' -Port 8787 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
        Launch-Research $state $oldResearch
    }
    if (!(Test-NetConnection -ComputerName '127.0.0.1' -Port 8330 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
        Stop-Host $state 'runtime' $state
        Launch-RuntimeAndBff $state $previousRepository (Join-Path $state 'runtime.toml') (Join-Path $state 'bff-venv/Scripts/python.exe')
    }
}

Wait-Http 'http://127.0.0.1:8330/health/ready'
Write-Output "Console is ready on http://127.0.0.1:8330/ ($Action)."
