[CmdletBinding()]
param(
  [switch]$AddToPath = $true
)

$ErrorActionPreference = "Stop"

# This emergency installer can run before Python exists. Emit the same bounded
# schema using static fields only; the installed diagnostics worker owns export
# and retention. Logging failure never changes installation or its exit status.
$diagnosticId = [Guid]::NewGuid().ToString('N')
$diagnosticStart = [Diagnostics.Stopwatch]::StartNew()
$diagnosticPhase = 'discovery'
$diagnosticPhaseStart = 0.0
function Write-InstallEvent([string]$Event, [string]$Severity, [string]$Status, [string]$ErrorType = '') {
  try {
    $diagnosticRoot = $env:VAWS_DIAGNOSTICS_ROOT
    if (-not $diagnosticRoot) { $diagnosticRoot = Join-Path $env:LOCALAPPDATA 'vaws/diagnostics' }
    $folder = Join-Path $diagnosticRoot 'events/vaws-workspace'
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    $record = @{
      schema = 1; timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffffZ')
      monotonic_ns = [long]($diagnosticStart.Elapsed.TotalMilliseconds * 1000000)
      clock_domain_unknown = $true; pid = $PID; process_instance_id = $diagnosticId
      component = 'vaws-workspace'; package_version = 'bootstrap'; severity = $Severity
      event = $Event; operation = 'install_gh_user'; operation_id = $diagnosticId; trace_id = $diagnosticId
      status = $Status; duration_ms = $diagnosticStart.Elapsed.TotalMilliseconds
      attributes = @{ stage = $diagnosticPhase; elapsed_ms = $diagnosticStart.Elapsed.TotalMilliseconds - $diagnosticPhaseStart }
    }
    if ($ErrorType) { $record.attributes.error_type = $ErrorType; $record.attributes.category = 'bootstrap' }
    $line = ConvertTo-Json -InputObject $record -Depth 4 -Compress
    if ($line.Length -le 16000) {
      [IO.File]::AppendAllText((Join-Path $folder "$PID-$diagnosticId.jsonl"), "$line`n", [Text.UTF8Encoding]::new($false))
    }
  } catch { }
}
trap {
  Write-InstallEvent 'operation.end' 'ERROR' 'error' $_.Exception.GetType().Name
  throw
}
Write-InstallEvent 'operation.start' 'INFO' 'running'

function Get-ArchToken {
  $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
  switch ($arch) {
    "x64"   { return "amd64" }
    "arm64" { return "arm64" }
    default { throw "Unsupported Windows architecture: $arch" }
  }
}

function Save-BoundedWeb([string]$Uri, [string]$Destination, [int]$Deadline = 180) {
  # A child job bounds DNS, connection and slow-drip reads, including PS 5.1.
  # This installer runs before Python exists and uses Windows system trust.
  $watch = [Diagnostics.Stopwatch]::StartNew()
  $job = Start-Job -ArgumentList $Uri,$Destination -ScriptBlock {
    param($Url,$Output)
    $ErrorActionPreference = 'Stop'
    $options = @{ Uri=$Url; OutFile=$Output; UseBasicParsing=$true; TimeoutSec=20;
      Headers=@{ 'User-Agent'='vaws-bootstrap' } }
    if ($env:HTTPS_PROXY) {
      $address = [UriBuilder]::new($env:HTTPS_PROXY)
      if ($address.UserName) {
        $secureValue = ConvertTo-SecureString ([Uri]::UnescapeDataString($address.Password)) -AsPlainText -Force
        $options.ProxyCredential = [PSCredential]::new([Uri]::UnescapeDataString($address.UserName),$secureValue)
      }
      $address.UserName=''; $address.Password=''
      $options.Proxy = $address.Uri.AbsoluteUri
    }
    Invoke-WebRequest @options | Out-Null
  }
  try {
    while (-not (Wait-Job -Job $job -Timeout 10)) {
      Write-Host ("GitHub download: {0:N0}s / {1}s" -f $watch.Elapsed.TotalSeconds,$Deadline)
      if ($watch.Elapsed.TotalSeconds -ge $Deadline) { throw 'Download deadline exceeded; retry after network check.' }
    }
    if ($job.State -ne 'Completed') { throw 'Download failed; inspect proxy, certificate trust and endpoint access.' }
    Receive-Job -Job $job -ErrorAction Stop | Out-Null
  } finally {
    if ($job.State -eq 'Running') { Stop-Job -Job $job }
    Remove-Job -Job $job -Force
  }
}

$archToken = Get-ArchToken
$apiUrl = "https://api.github.com/repos/cli/cli/releases/latest"
$headers = @{
  "Accept" = "application/vnd.github+json"
  "User-Agent" = "repo-init-fallback"
}

$diagnosticPhase = 'release_lookup'
$diagnosticPhaseStart = $diagnosticStart.Elapsed.TotalMilliseconds

Write-Host "Querying latest GitHub CLI release ..."
$metadataFile = Join-Path $env:TEMP ('vaws-gh-release-' + [Guid]::NewGuid().ToString('N') + '.json')
try {
  Save-BoundedWeb $apiUrl $metadataFile 30
  $release = Get-Content -LiteralPath $metadataFile -Raw | ConvertFrom-Json
} finally { if (Test-Path -LiteralPath $metadataFile) { Remove-Item -LiteralPath $metadataFile -Force } }
$asset = $release.assets | Where-Object { $_.name -match ("^gh_.*_windows_{0}\.zip$" -f $archToken) } | Select-Object -First 1

if (-not $asset) {
  throw "Could not find a matching Windows asset for architecture $archToken"
}
if ($release.tag_name -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { throw 'Unexpected release tag' }
$checksumAsset = $release.assets | Where-Object { $_.name -match '_checksums\.txt$' } | Select-Object -First 1
if (-not $checksumAsset) { throw 'Release has no published checksums' }
$checksumFile = Join-Path $env:TEMP ('vaws-gh-checksums-' + [Guid]::NewGuid().ToString('N') + '.txt')
try {
  Save-BoundedWeb $checksumAsset.browser_download_url $checksumFile 30
  $checksumLine = Get-Content -LiteralPath $checksumFile | Where-Object { $_ -match ('^[0-9a-f]{64}\s+\*?' + [regex]::Escape($asset.name) + '$') } | Select-Object -First 1
  if (-not $checksumLine) { throw 'No SHA256 digest for selected asset' }
  $expectedHash = ($checksumLine -split '\s+')[0]
} finally { if (Test-Path -LiteralPath $checksumFile) { Remove-Item -LiteralPath $checksumFile -Force } }
Write-InstallEvent 'phase.end' 'INFO' 'success'

$installRoot = Join-Path $env:LOCALAPPDATA "Programs\GitHubCLI\$($release.tag_name)"
$binDir = Join-Path $installRoot "bin"
$currentDir = Join-Path $env:LOCALAPPDATA "Programs\GitHubCLI\current"
$tmpZip = Join-Path $env:TEMP ('vaws-gh-' + [Guid]::NewGuid().ToString('N') + '.zip')
$tmpExtract = Join-Path $env:TEMP ("repo-init-gh-" + [System.Guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Force -Path $binDir | Out-Null
New-Item -ItemType Directory -Force -Path $currentDir | Out-Null
New-Item -ItemType Directory -Force -Path $tmpExtract | Out-Null

Write-Host "Downloading $($asset.name) ..."
$diagnosticPhase = 'download_install'
$diagnosticPhaseStart = $diagnosticStart.Elapsed.TotalMilliseconds
Save-BoundedWeb $asset.browser_download_url $tmpZip 300
if ((Get-FileHash -LiteralPath $tmpZip -Algorithm SHA256).Hash -ne $expectedHash) { throw 'Downloaded archive SHA256 mismatch' }
Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force

$ghExe = Get-ChildItem -Path $tmpExtract -Recurse -Filter gh.exe | Where-Object {
  $_.FullName -match "\\bin\\gh\.exe$"
} | Select-Object -First 1

if (-not $ghExe) {
  throw "Downloaded archive does not contain bin\gh.exe"
}

Copy-Item -Force $ghExe.FullName (Join-Path $binDir "gh.exe")
Copy-Item -Force (Join-Path $binDir "gh.exe") (Join-Path $currentDir "gh.exe")
Write-InstallEvent 'phase.end' 'INFO' 'success'
$diagnosticPhase = 'path_and_cleanup'
$diagnosticPhaseStart = $diagnosticStart.Elapsed.TotalMilliseconds

if ($AddToPath) {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $pathEntries = @()
  if ($userPath) {
    $pathEntries = $userPath -split ";"
  }
  if ($pathEntries -notcontains $currentDir) {
    $newPath = if ($userPath) { "$userPath;$currentDir" } else { $currentDir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Updated the user PATH with: $currentDir"
    Write-Host "Restart your terminal so the new PATH is visible."
  }
}

Remove-Item -LiteralPath $tmpZip -Force
$resolvedExtract = [IO.Path]::GetFullPath($tmpExtract)
$resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
if (-not $resolvedExtract.StartsWith($resolvedTemp,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected extraction directory' }
Remove-Item -LiteralPath $resolvedExtract -Recurse -Force

Write-Host ""
Write-Host "Installed gh to $binDir"
Write-Host "Convenience path: $currentDir\gh.exe"
Write-Host ""
Write-Host "Verify with:"
Write-Host "  gh --version"
Write-Host "  gh auth status --hostname github.com"
Write-InstallEvent 'phase.end' 'INFO' 'success'
Write-InstallEvent 'operation.end' 'INFO' 'success'
