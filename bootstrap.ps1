# Starts the local Paradee 1.3 Dialogue Studio on Windows x64.
# Reuses a working .venv, or builds portable Python 3.10.11 when .venv is missing.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$StdPy = Join-Path $Venv "Scripts\python.exe"
$EmbedPy = Join-Path $Venv "python.exe"
$Server = Join-Path $Root "server.py"
$EmbedZipUrl = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$ParadeeZip = "https://github.com/sahilmahendrakar/paradee/archive/af5ca1a17daad860d1aea60510d93d8dbd1d7b6f.zip"

function Test-Paradee([string]$Python) {
    if (-not (Test-Path -LiteralPath $Python)) {
        return @{ Ok = $false; Missing = $true; Output = "" }
    }
    $output = & $Python -c "import paradee" 2>&1 | Out-String
    return @{ Ok = ($LASTEXITCODE -eq 0); Missing = $false; Output = $output.Trim() }
}

function Invoke-Checked([string]$Exe, [string[]]$ArgList) {
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe failed with exit code $LASTEXITCODE"
    }
}

function Ensure-SpacyModel([string]$Python) {
    & $Python -c "import spacy.util; raise SystemExit(0 if spacy.util.is_package('en_core_web_sm') else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing the English spaCy model en_core_web_sm..."
        Invoke-Checked $Python @("-m", "spacy", "download", "en_core_web_sm")
    }
}

function Install-Paradee([string]$Python) {
    # The embeddable Python ._pth file hides pip's temporary build environment,
    # so hatchling must be installed here and the package built without isolation.
    Write-Host "Installing Paradee 1.3. This can take several minutes..."
    Invoke-Checked $Python @("-m", "pip", "install", "--no-warn-script-location", "hatchling")
    Invoke-Checked $Python @("-m", "pip", "install", "--no-warn-script-location", "--no-build-isolation", $ParadeeZip)
    Ensure-SpacyModel $Python
}

function Install-PortablePython {
    Write-Host "Downloading portable Python 3.10.11..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $zip = Join-Path $env:TEMP "python-3.10.11-embed-amd64.zip"
    $pipScript = Join-Path $env:TEMP "paradee-get-pip.py"
    Invoke-WebRequest -Uri $EmbedZipUrl -OutFile $zip -UseBasicParsing
    Write-Host "Extracting Python into .venv..."
    Expand-Archive -LiteralPath $zip -DestinationPath $Venv -Force
    @"
python310.zip
.
Lib
Lib\site-packages
import site
"@ | Set-Content -LiteralPath (Join-Path $Venv "python310._pth") -Encoding ASCII
    New-Item -ItemType Directory -Force -Path (Join-Path $Venv "Lib\site-packages") | Out-Null
    if (-not (Test-Path -LiteralPath $EmbedPy)) {
        throw "Portable Python did not extract python.exe into .venv."
    }
    Write-Host "Installing pip..."
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $pipScript -UseBasicParsing
    Invoke-Checked $EmbedPy @($pipScript, "--no-warn-script-location")
    Install-Paradee $EmbedPy
}

try {
    if (-not (Test-Path -LiteralPath $Server)) {
        throw "server.py was not found next to bootstrap.ps1."
    }

    $std = Test-Paradee $StdPy
    $embed = $null
    $Python = $null
    if ($std.Ok) {
        $Python = $StdPy
    } else {
        $embed = Test-Paradee $EmbedPy
        if ($embed.Ok) {
            $Python = $EmbedPy
        }
    }

    if (-not $Python) {
        if (Test-Path -LiteralPath $EmbedPy) {
            Install-Paradee $EmbedPy
            $Python = $EmbedPy
        } elseif (-not (Test-Path -LiteralPath $Venv)) {
            Install-PortablePython
            $Python = $EmbedPy
        } else {
            Write-Host ".venv is already here, but Paradee could not be imported."
            Write-Host "Looked for:"
            Write-Host "  $StdPy"
            Write-Host "  $EmbedPy"
            if (-not $std.Missing -and $std.Output) {
                Write-Host $std.Output
            }
            if ($embed -and -not $embed.Missing -and $embed.Output) {
                Write-Host $embed.Output
            }
            Write-Host "This launcher will not delete that folder."
            exit 1
        }
        $ready = Test-Paradee $Python
        if (-not $ready.Ok) {
            if ($ready.Output) { Write-Host $ready.Output }
            throw "Paradee was installed, but import paradee still failed."
        }
    }

    Ensure-SpacyModel $Python
    Write-Host "Using $Python"
    Write-Host "Starting Paradee 1.3 Dialogue Studio at http://127.0.0.1:8765"
    $waiter = [powershell]::Create().AddScript({
        $deadline = (Get-Date).AddSeconds(60)
        while ((Get-Date) -lt $deadline) {
            $client = New-Object System.Net.Sockets.TcpClient
            try {
                $wait = $client.BeginConnect("127.0.0.1", 8765, $null, $null)
                if ($wait.AsyncWaitHandle.WaitOne(400)) {
                    $client.EndConnect($wait)
                    Start-Process "http://127.0.0.1:8765"
                    return
                }
            } catch {
            } finally {
                $client.Close()
            }
            Start-Sleep -Milliseconds 300
        }
        Start-Process "http://127.0.0.1:8765"
    })
    $waiter.Runspace = [runspacefactory]::CreateRunspace()
    $waiter.Runspace.Open()
    [void]$waiter.BeginInvoke()

    & $Python $Server
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} catch {
    Write-Host $_.Exception.Message
    exit 1
}
