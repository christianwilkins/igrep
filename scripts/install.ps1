param(
    [string]$Version = "latest",
    [string]$InstallDir = "$env:USERPROFILE\\bin"
)

$ErrorActionPreference = "Stop"
$repo = "christianwilkins/igrep"
$asset = "igrep-windows-amd64.zip"

if ($Version -eq "latest") {
    $downloadUrl = "https://github.com/$repo/releases/latest/download/$asset"
}
else {
    $downloadUrl = "https://github.com/$repo/releases/download/$Version/$asset"
}

$tempDir = New-Item -ItemType Directory -Path ([System.IO.Path]::GetTempPath()) -Name ("igrep-" + [System.Guid]::NewGuid())
$zipPath = Join-Path $tempDir.FullName $asset

Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath
Expand-Archive -Path $zipPath -DestinationPath $tempDir.FullName -Force

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item (Join-Path $tempDir.FullName "igrep.exe") (Join-Path $InstallDir "igrep.exe") -Force

Write-Host "Installed igrep to $InstallDir\\igrep.exe"
Write-Host "Add $InstallDir to PATH if needed"
