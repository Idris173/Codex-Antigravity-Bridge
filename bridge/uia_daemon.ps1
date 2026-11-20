$ErrorActionPreference = "Stop"
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase, System.Windows.Forms

$dllPath = Join-Path $PSScriptRoot "BridgeFastUia.dll"
$csPath  = Join-Path $PSScriptRoot "BridgeFastUia.cs"
if (Test-Path $dllPath) {
    [void][System.Reflection.Assembly]::Load([System.IO.File]::ReadAllBytes($dllPath))
} else {
    Add-Type -Path $csPath -ReferencedAssemblies "UIAutomationClient","UIAutomationTypes","WindowsBase","System.Core" -OutputAssembly $dllPath
    [void][System.Reflection.Assembly]::Load([System.IO.File]::ReadAllBytes($dllPath))
}

$driverPath = Join-Path $PSScriptRoot "uia_driver.ps1"
$loadedDllTicks = if (Test-Path $dllPath) { (Get-Item $dllPath).LastWriteTimeUtc.Ticks } else { [long]0 }

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $trimmed = $line.Trim()
    if ($trimmed -eq "") { continue }
    if ($trimmed -eq "EXIT") { break }

    try {
        $req = $trimmed | ConvertFrom-Json
        $splat = @{
            Action     = [string]$req.action
            OutputFile = [string]$req.output_file
            TimeoutSec = if ($req.timeout_sec) { [int]$req.timeout_sec } else { 180 }
            AllowPlaceholderTitle = [bool]$req.allow_placeholder_title
        }
        if ($req.expected_session) { $splat["ExpectedSession"] = [string]$req.expected_session }
        if ($req.project_name)     { $splat["ProjectName"]     = [string]$req.project_name }
        if ($req.session_name)     { $splat["SessionName"]     = [string]$req.session_name }
        if ($req.target_hwnd)      { $splat["TargetHwnd"]      = [int]$req.target_hwnd }
        if ($req.target_pid)       { $splat["TargetPid"]       = [int]$req.target_pid }
        if ($req.dispatch_id)      { $splat["DispatchId"]      = [string]$req.dispatch_id }
        if ($req.payload_file)     { $splat["PayloadFile"]     = [string]$req.payload_file }

        & $driverPath @splat *> $null
        [Console]::Out.WriteLine('{"daemon_ack":true}')
        [Console]::Out.Flush()
        if ((Test-Path $dllPath) -and ((Get-Item $dllPath).LastWriteTimeUtc.Ticks -ne $loadedDllTicks)) {
            break
        }
    } catch {
        $failure = @{ daemon_ack = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
        [Console]::Out.WriteLine($failure)
        [Console]::Out.Flush()
    }
}
