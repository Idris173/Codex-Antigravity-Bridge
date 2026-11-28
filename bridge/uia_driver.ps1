param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("inspect", "read_latest", "dispatch", "new_project_session", "switch_session")]
    [string]$Action,

    [string]$PayloadFile = "",
    [string]$OutputFile = "",
    [string]$ExpectedSession = "",
    [string]$ProjectName = "",
    [string]$SessionName = "",
    [string]$DispatchId = "",
    [int]$TargetHwnd = 0,
    [int]$TargetPid = 0,
    [int]$TimeoutSec = 180,
    [bool]$AllowPlaceholderTitle = $(
        if ($null -ne $env:CODEX_ANTIGRAVITY_BRIDGE_ALLOW_PLACEHOLDER_TITLE) {
            $env:CODEX_ANTIGRAVITY_BRIDGE_ALLOW_PLACEHOLDER_TITLE -eq "1"
        } else {
            $env:CODEX_DIRECTOR_ALLOW_PLACEHOLDER_TITLE -eq "1"
        }
    )
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase, System.Windows.Forms

$dllPath = Join-Path $PSScriptRoot "BridgeFastUia.dll"
$csPath  = Join-Path $PSScriptRoot "BridgeFastUia.cs"
$loadedUiaType = "BridgeFastUia" -as [type]
if ($null -eq $loadedUiaType) {
    if (Test-Path $dllPath) {
        [void][System.Reflection.Assembly]::Load([System.IO.File]::ReadAllBytes($dllPath))
    } else {
        Add-Type -Path $csPath -ReferencedAssemblies "UIAutomationClient","UIAutomationTypes","WindowsBase","System.Core" -OutputAssembly $dllPath
        [void][System.Reflection.Assembly]::Load([System.IO.File]::ReadAllBytes($dllPath))
    }
} elseif ($null -eq $loadedUiaType.GetMethod("InvokeProjectNewChat")) {
    # Self-heal when invoked inside a pre-upgrade uia_daemon.ps1 AppDomain holding an older BridgeFastUia type
    $childArgs = @(
        "-Sta", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath,
        "-Action", $Action, "-TimeoutSec", [string]$TimeoutSec
    )
    if ($PayloadFile)    { $childArgs += @("-PayloadFile", $PayloadFile) }
    if ($OutputFile)     { $childArgs += @("-OutputFile", $OutputFile) }
    if ($ExpectedSession){ $childArgs += @("-ExpectedSession", $ExpectedSession) }
    if ($ProjectName)    { $childArgs += @("-ProjectName", $ProjectName) }
    if ($SessionName)    { $childArgs += @("-SessionName", $SessionName) }
    if ($DispatchId)     { $childArgs += @("-DispatchId", $DispatchId) }
    if ($TargetHwnd)     { $childArgs += @("-TargetHwnd", [string]$TargetHwnd) }
    if ($TargetPid)      { $childArgs += @("-TargetPid", [string]$TargetPid) }
    $prevEnv = $env:CODEX_ANTIGRAVITY_BRIDGE_ALLOW_PLACEHOLDER_TITLE
    try {
        $env:CODEX_ANTIGRAVITY_BRIDGE_ALLOW_PLACEHOLDER_TITLE = if ($AllowPlaceholderTitle) { "1" } else { "0" }
        & powershell.exe @childArgs
    } finally {
        $env:CODEX_ANTIGRAVITY_BRIDGE_ALLOW_PLACEHOLDER_TITLE = $prevEnv
    }
    return
}

function Write-JsonResult([hashtable]$Data) {
    $json = $Data | ConvertTo-Json -Depth 8 -Compress
    if ($OutputFile -and $OutputFile.Trim() -ne "") {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($OutputFile, $json, $utf8NoBom)
    }
    Write-Output $json
}

function Read-SharedTailText([string]$Path, [long]$Offset) {
    $fs = $null
    try {
        $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
        $fs = New-Object System.IO.FileStream($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
        if ($fs.Length -le $Offset) { return @{ text = ""; new_offset = $fs.Length } }
        [void]$fs.Seek($Offset, [System.IO.SeekOrigin]::Begin)
        $len = [int][Math]::Min(4194304, ($fs.Length - $Offset))
        $buf = New-Object byte[] $len
        $readBytes = $fs.Read($buf, 0, $len)
        $lastLf = $readBytes - 1
        while ($lastLf -ge 0 -and $buf[$lastLf] -ne 10) { $lastLf-- }
        if ($lastLf -lt 0) { return @{ text = ""; new_offset = $Offset } }
        $txt = [System.Text.Encoding]::UTF8.GetString($buf, 0, $lastLf + 1)
        return @{ text = $txt; new_offset = ($Offset + $lastLf + 1) }
    } catch {
        return @{ text = ""; new_offset = $Offset }
    } finally {
        if ($null -ne $fs) { $fs.Dispose() }
    }
}

function Get-CodexRolloutBaseline {
    $sessionsRoot = Join-Path $env:USERPROFILE ".codex\sessions"
    $map = @{}
    if (-not (Test-Path $sessionsRoot)) { return $map }

    $now = Get-Date
    $dates = @($now, $now.ToUniversalTime(), $now.AddDays(-1))
    $visitedDirs = New-Object System.Collections.Generic.HashSet[string]

    foreach ($d in $dates) {
        $dir = Join-Path $sessionsRoot ("{0:D4}\{1:D2}\{2:D2}" -f $d.Year, $d.Month, $d.Day)
        if ($visitedDirs.Add($dir) -and (Test-Path $dir)) {
            $files = Get-ChildItem -LiteralPath $dir -Filter "rollout-*.jsonl" -File -ErrorAction SilentlyContinue
            foreach ($f in $files) {
                $map[$f.FullName] = [long]$f.Length
            }
        }
    }
    return $map
}

function IsPlaceholderPrompt([string]$Title) {
    $value = $Title.Trim()
    return ($value -eq "" -or
        $value -match '^(新聊天|新建聊天|New chat|Codex|ChatGPT|你说：|You said:|ChatGPT 说：|ChatGPT said:|\[SUMMARY\]|\[CONSTRAINTS\]|\d{1,2}:\d{2}(\s?[AP]M)?)$' -or
        $value -match '开始新聊天|你想让我们在|我们应该在|What should we')
}

function Resolve-TargetWindows([int]$RequestedHwnd) {
    $handles = if ($RequestedHwnd -ne 0) { @([IntPtr]$RequestedHwnd) } else {
        [BridgeFastUia]::FindChatGptWindowHandlesFast()
    }
    foreach ($h in $handles) {
        try {
            if (-not [BridgeFastUia]::IsWindow($h)) { continue }
            $pidVal = [uint32]0
            [void][BridgeFastUia]::GetWindowThreadProcessId($h, [ref]$pidVal)
            $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
            if (-not $proc -or $proc.ProcessName -notmatch '^(ChatGPT|Codex)$') { continue }
            if ($TargetPid -eq 0 -or $TargetPid -eq $pidVal) {
                if ([BridgeFastUia]::IsIconic($h)) {
                    [BridgeFastUia]::RestoreWithoutActivation($h)
                    Start-Sleep -Milliseconds 150
                }
            }
            $el = [System.Windows.Automation.AutomationElement]::FromHandle($h)
            if ($null -eq $el -or $el.Current.ProcessId -ne $pidVal) { continue }
            [PSCustomObject]@{
                Element      = $el
                Hwnd         = $h.ToInt32()
                Pid          = $pidVal
                ProcessStart = $proc.StartTime.ToFileTimeUtc()
                ProcessName  = $proc.ProcessName
                WindowTitle  = [string]$el.Current.Name
            }
        } catch { continue }
    }
}

function Build-Snapshot($WinInfo, [bool]$IncludeSidebar) {
    $scan = [BridgeFastUia]::ScanWindowFast($WinInfo.Element, $IncludeSidebar)
    $mainTexts = $scan.MainTexts

    $activeTitle = ""
    $cleanWinTitle = ([string]$WinInfo.Element.Current.Name -replace '\s*[-—|]\s*ChatGPT.*$', '').Trim()
    if ($scan.HeaderTitle -ne "") {
        $activeTitle = $scan.HeaderTitle
    } elseif ($cleanWinTitle -ne "" -and $cleanWinTitle -notmatch '^(ChatGPT|Codex)$') {
        $activeTitle = $cleanWinTitle
    } elseif ($mainTexts.Count -gt 0) {
        $activeTitle = $mainTexts[0].Substring(0, [Math]::Min(32, $mainTexts[0].Length))
    } else {
        $activeTitle = "新聊天"
    }

    $joinedStream = [string]::Join("`n", $mainTexts)
    $tailPreview = if ($joinedStream.Length -gt 0) {
        $joinedStream.Substring([Math]::Max(0, $joinedStream.Length - 1500))
    } else { "" }

    $lastReply = $tailPreview
    $lastCloseIdx = $joinedStream.LastIndexOf("[/DIRECTOR_TASK]", [StringComparison]::Ordinal)
    $searchFrom = if ($lastCloseIdx -ge 0) { $lastCloseIdx + "[/DIRECTOR_TASK]".Length } else { 0 }
    $lastSummary = [regex]::Matches($joinedStream.Substring($searchFrom), $script:summaryLine) | Select-Object -Last 1
    $lastSummaryIdx = if ($null -ne $lastSummary) { $searchFrom + $lastSummary.Index } else { -1 }
    $lastAssistantMarker = -1
    foreach ($marker in @("最新一条回复`n", "Latest reply`n", "ChatGPT 说：`n", "ChatGPT said:`n")) {
        $idx = $joinedStream.LastIndexOf($marker)
        if ($idx -ge 0) {
            $pos = $idx + $marker.Length
            if ($pos -gt $lastAssistantMarker) { $lastAssistantMarker = $pos }
        }
    }

    if ($lastSummaryIdx -ge 0 -and $lastSummaryIdx -ge $lastAssistantMarker) {
        $lastReply = $joinedStream.Substring($lastSummaryIdx)
    } elseif ($lastAssistantMarker -ge 0 -and $lastAssistantMarker -lt $joinedStream.Length) {
        $lastReply = $joinedStream.Substring($lastAssistantMarker).Trim()
    } elseif ($joinedStream.Length -gt 0) {
        $lastReply = $joinedStream.Substring([Math]::Max(0, $joinedStream.Length - 8000))
    }

    $recentPreviewList = @()
    $takeStart = [Math]::Max(0, $mainTexts.Count - 6)
    for ($i = $takeStart; $i -lt $mainTexts.Count; $i++) {
        $s = $mainTexts[$i]
        if ($s.Length -gt 140) { $s = $s.Substring(0, 140) + "..." }
        $recentPreviewList += $s
    }

    $headSeed = if ($mainTexts.Count -gt 0) { $mainTexts[0].Substring(0, [Math]::Min(40, $mainTexts[0].Length)) } else { "EMPTY" }

    return @{
        hwnd               = $WinInfo.Hwnd
        pid                = $WinInfo.Pid
        process_start      = $WinInfo.ProcessStart
        process_name       = $WinInfo.ProcessName
        window_title       = [string]$WinInfo.Element.Current.Name
        is_minimized       = [BridgeFastUia]::IsIconic([IntPtr]$WinInfo.Hwnd)
        active_session     = $activeTitle
        title_authoritative = ($scan.TitleAuthoritative -and -not (IsPlaceholderPrompt $activeTitle))
        sidebar_sessions   = @($scan.SidebarSessions)
        recent_messages    = $recentPreviewList
        last_message       = $lastReply
        full_stream_length = $joinedStream.Length
        is_generating      = $scan.IsGenerating
        has_prompt_box     = ($null -ne $scan.PromptElement)
        scanned_nodes      = $scan.ScannedCount
        fingerprint        = "$($WinInfo.Hwnd)|$activeTitle|$headSeed"
        _prompt_elem       = $scan.PromptElement
        _full_stream       = $joinedStream
        _sidebar_x         = $scan.SidebarBoundaryX
        _main_right_x      = $scan.MainRightBoundaryX
    }
}

function Strip-InternalFields([hashtable]$Snap) {
    $Snap.Remove("_prompt_elem")  | Out-Null
    $Snap.Remove("_full_stream")  | Out-Null
    $Snap.Remove("_sidebar_x")    | Out-Null
    $Snap.Remove("_main_right_x") | Out-Null
}

$script:summaryLine = '(?m)^[^\S\r\n]*(?:#+[^\S\r\n]*)?\[SUMMARY\][^\S\r\n]*\r?$'

function Get-DispatchUiTail([string]$Stream, [string]$Anchor) {
    $anchorIdx = $Stream.LastIndexOf($Anchor, [StringComparison]::Ordinal)
    if ($anchorIdx -lt 0) { return "" }
    $closeIdx = $Stream.IndexOf("[/DIRECTOR_TASK]", $anchorIdx, [StringComparison]::Ordinal)
    if ($closeIdx -lt 0) { return "" }
    $tail = $Stream.Substring($closeIdx + "[/DIRECTOR_TASK]".Length)
    $nextUser = [regex]::Match($tail, '(?im)^[^\S\r\n]*(?:你说：|You said:|\[DIRECTOR_TASK\])[^\S\r\n]*\r?$')
    if ($nextUser.Success) { return $tail.Substring(0, $nextUser.Index) }
    return $tail
}

function Focus-Composer($Window, $Prompt) {
    $handle = [IntPtr]$Window.Hwnd
    if ([BridgeFastUia]::GetForegroundWindow() -ne $handle) {
        $waitedMs = 0
        while ([BridgeFastUia]::GetIdleMilliseconds() -lt 1000 -and $waitedMs -lt 5000) {
            Start-Sleep -Milliseconds 150
            $waitedMs += 150
        }
        if ([BridgeFastUia]::GetIdleMilliseconds() -lt 900) { return "USER_ACTIVE_INPUT" }
        if (-not [BridgeFastUia]::ActivateWindowReliable($handle)) { return "FOCUS_BLOCKED" }
    }
    try { $Prompt.SetFocus() } catch { return "FOCUS_BLOCKED" }
    if ([BridgeFastUia]::GetForegroundWindow() -ne $handle -or -not [BridgeFastUia]::HasComposerFocus($Prompt)) {
        return "FOCUS_BLOCKED"
    }
    return ""
}

function Write-FocusError([string]$Code) {
    Write-JsonResult @{
        ok = $false
        error_code = $Code
        message = if ($Code -eq "USER_ACTIVE_INPUT") { "检测到前台有键鼠操作，暂缓需要前台按键的投递。" }
                  else { "未能聚焦目标 ChatGPT 输入框，无法安全发送前台按键。" }
    }
}

# --- Entry Dispatch ---
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$windows = @(Resolve-TargetWindows $TargetHwnd)

if ($windows.Count -eq 0) {
    if ($TargetHwnd -ne 0) {
        Write-JsonResult @{ ok = $false; error_code = "TARGET_WINDOW_NOT_FOUND"; hwnd = $TargetHwnd; message = "指定的 ChatGPT 窗口已失效或不可访问。" }
        return
    }
    $runningProcs = @(Get-Process | Where-Object { $_.ProcessName -match '^(ChatGPT|Codex|codex)$' })
    if ($runningProcs.Count -gt 0) {
        Write-JsonResult @{
            ok            = $false
            error_code    = "WINDOW_NOT_ACCESSIBLE"
            process_count = $runningProcs.Count
            elapsed_ms    = $sw.ElapsedMilliseconds
            message       = "检测到 ChatGPT/Codex 进程已运行，但窗口未显示在桌面。"
            windows       = @()
        }
        return
    }
    Write-JsonResult @{
        ok         = $false
        error_code = "APP_NOT_RUNNING"
        elapsed_ms = $sw.ElapsedMilliseconds
        message    = "未检测到正在运行的 ChatGPT 桌面应用窗口，请先打开 ChatGPT 应用。"
        windows    = @()
    }
    return
}

$targetWin = $windows[0]
if ($TargetPid -ne 0 -and $targetWin.Pid -ne $TargetPid) {
    Write-JsonResult @{ ok = $false; error_code = "PID_MISMATCH"; message = "目标窗口进程 PID 已变更，请重新绑定会话。" }
    return
}

if ($Action -in @("new_project_session", "switch_session")) {
    $inProject = $Action -eq "new_project_session"
    [string[]]$names = if ($inProject) {
        @("在 $ProjectName 中开始新聊天", "Start new chat in $ProjectName", "在 $ProjectName 中创建新聊天",
          "New chat in $ProjectName", "Create new chat in $ProjectName")
    } else { @($SessionName) }

    function Invoke-TargetSidebarAction($Snapshot) {
        if ($inProject) {
            return [BridgeFastUia]::InvokeProjectNewChat($targetWin.Element, $Snapshot._sidebar_x, $ProjectName, $names)
        }
        return [BridgeFastUia]::InvokeSidebarItem($targetWin.Element, $Snapshot._sidebar_x, $false, $names)
    }

    $snap = Build-Snapshot $targetWin $false
    $clicked = Invoke-TargetSidebarAction $snap
    if ($null -eq $clicked -and [BridgeFastUia]::TryExpandSidebar($targetWin.Element)) {
        Start-Sleep -Milliseconds 300
        $snap = Build-Snapshot $targetWin $false
        $clicked = Invoke-TargetSidebarAction $snap
    }
    if ($null -ne $clicked) {
        Start-Sleep -Milliseconds 500
        $snap = Build-Snapshot $targetWin $false
    }
    Strip-InternalFields $snap
    Write-JsonResult @{
        ok = ($null -ne $clicked)
        button = $clicked
        elapsed_ms = $sw.ElapsedMilliseconds
        snapshot = $snap
        message = if ($inProject) {
            if ($null -ne $clicked) { "已在 '$ProjectName' 下开启新会话。" } else { "未找到工程 '$ProjectName' 的新建聊天按钮。" }
        } else {
            if ($null -ne $clicked) { "已切换到会话 '$clicked'。" } else { "未在侧边栏找到会话 '$SessionName'。" }
        }
    }
    return
}

if ($Action -eq "inspect") {
    $winSnapshots = @()
    foreach ($w in $windows) {
        $snap = Build-Snapshot $w $true
        Strip-InternalFields $snap
        $winSnapshots += $snap
    }
    Write-JsonResult @{
        ok           = $true
        elapsed_ms   = $sw.ElapsedMilliseconds
        window_count = $winSnapshots.Count
        windows      = $winSnapshots
    }
    return
}

$snapBefore = Build-Snapshot $targetWin $false

if ($ExpectedSession -ne "") {
    $exp = $ExpectedSession.Trim()
    $act = [string]$snapBefore.active_session
    $winT = [string]$snapBefore.window_title
    if (-not $AllowPlaceholderTitle -and -not (IsPlaceholderPrompt $exp) -and $snapBefore.title_authoritative) {
        $cmp = [StringComparison]::OrdinalIgnoreCase
        $matched = (-not (IsPlaceholderPrompt $act) -and ($act.IndexOf($exp, $cmp) -ge 0 -or $exp.IndexOf($act, $cmp) -ge 0))
        if (-not $matched -and $winT.IndexOf($exp, $cmp) -lt 0) {
            Write-JsonResult @{
                ok = $false
                error_code = "SESSION_MISMATCH"
                message = "当前激活会话为 '$act'，与锁定的 '$exp' 不一致。"
                active_session = $act
                expected = $exp
            }
            return
        }
    }
}

if ($Action -eq "read_latest") {
    Strip-InternalFields $snapBefore
    Write-JsonResult @{
        ok         = $true
        elapsed_ms = $sw.ElapsedMilliseconds
        snapshot   = $snapBefore
    }
    return
}

if ($Action -eq "dispatch") {
    if (-not $PayloadFile -or -not (Test-Path $PayloadFile)) {
        Write-JsonResult @{ ok = $false; error_code = "MISSING_PAYLOAD"; message = "缺少 PayloadFile。" }
        return
    }
    if ($snapBefore.is_generating) {
        Write-JsonResult @{ ok = $false; error_code = "BUSY_GENERATING"; message = "目标会话正在生成回复中，请稍候。" }
        return
    }
    $promptElem = $snapBefore._prompt_elem
    if ($null -eq $promptElem) {
        Write-JsonResult @{ ok = $false; error_code = "PROMPT_BOX_NOT_FOUND"; message = "未定位到底部聊天输入框。" }
        return
    }

    $payloadText    = [System.IO.File]::ReadAllText($PayloadFile, [System.Text.Encoding]::UTF8)
    $baselineStream = [string]$snapBefore._full_stream
    $sidebarX       = $snapBefore._sidebar_x
    $mainRightX     = $snapBefore._main_right_x

    # Exact turn correlation marker (DispatchId takes precedence over STEP_GOAL)
    $dispatchMarker = if ($DispatchId -ne "") { "[DISPATCH_ID: $DispatchId]" } else { "" }
    $goalMatch      = [regex]::Match($payloadText, '\[STEP_GOAL\]\s*([^\r\n]+)')
    $promptAnchor   = if ($dispatchMarker -ne "") { $dispatchMarker } elseif ($goalMatch.Success) { $goalMatch.Groups[1].Value.Trim() } else { "[DIRECTOR_TASK]" }
    $rolloutBaseline = Get-CodexRolloutBaseline

    $origFgHwnd = [BridgeFastUia]::GetForegroundWindow()
    $usedForeground = $false

    $usedSilentValuePattern = $false
    $valPattern = $null
    if ($promptElem.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$valPattern)) {
        try {
            if (-not $valPattern.Current.IsReadOnly) {
                $valPattern.SetValue($payloadText)
                Start-Sleep -Milliseconds 90
                $usedSilentValuePattern = ([string]$valPattern.Current.Value -ceq $payloadText)
            }
        } catch { $usedSilentValuePattern = $false }
    }

    $clipboardWritten = $false
    $savedClipboard = $null
    try {
        if (-not $usedSilentValuePattern) {
            $usedForeground = $true
            $focusError = Focus-Composer $targetWin $promptElem
            if ($focusError) {
                Write-FocusError $focusError
                return
            }
            try {
                $clipboard = [System.Windows.Forms.Clipboard]::GetDataObject()
                if ($null -ne $clipboard) {
                    $formats = $clipboard.GetFormats($false)
                    if ($formats.Count -gt 0) {
                        $savedClipboard = New-Object System.Windows.Forms.DataObject
                        foreach ($format in $formats) {
                            $data = $clipboard.GetData($format, $false)
                            if ($null -eq $data) { throw "Cannot copy clipboard format $format" }
                            $savedClipboard.SetData($format, $false, $data)
                        }
                    }
                }
                [System.Windows.Forms.Clipboard]::SetText($payloadText)
                $clipboardWritten = $true
            } catch {
                Write-JsonResult @{ ok = $false; error_code = "CLIPBOARD_UNAVAILABLE"; message = "无法完整备份或写入剪贴板，尚未发送消息。" }
                return
            }

            if ([BridgeFastUia]::GetForegroundWindow() -ne [IntPtr]$targetWin.Hwnd) {
                Write-FocusError "FOCUS_BLOCKED"
                return
            }
            [System.Windows.Forms.SendKeys]::SendWait("^a")
            Start-Sleep -Milliseconds 30
            [System.Windows.Forms.SendKeys]::SendWait("^v")
            Start-Sleep -Milliseconds 120
        }

        $pRect = $promptElem.Current.BoundingRectangle
        if (-not [BridgeFastUia]::TryClickComposerSendButton($targetWin.Element, $sidebarX, $mainRightX, $pRect.Top)) {
            $usedForeground = $true
            $focusError = Focus-Composer $targetWin $promptElem
            if ($focusError) {
                Write-FocusError $focusError
                return
            }
            [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
        }
    } finally {
        if ($clipboardWritten) {
            Start-Sleep -Milliseconds 60
            try {
                if ([System.Windows.Forms.Clipboard]::GetText() -ceq $payloadText) {
                    if ($null -ne $savedClipboard) {
                        [System.Windows.Forms.Clipboard]::SetDataObject($savedClipboard, $true)
                    } else {
                        [System.Windows.Forms.Clipboard]::Clear()
                    }
                }
            } catch {}
        }

        if ($usedForeground -and $origFgHwnd -ne [IntPtr]::Zero -and $origFgHwnd -ne [IntPtr]$targetWin.Hwnd -and
            [BridgeFastUia]::GetForegroundWindow() -eq [IntPtr]$targetWin.Hwnd) {
            [BridgeFastUia]::ActivateWindowReliable($origFgHwnd) | Out-Null
        }
    }

    $injectDoneMs = $sw.ElapsedMilliseconds

    # Exact turn-correlated 35ms JSONL pickup + UIA fallback
    $deadline       = (Get-Date).AddSeconds($TimeoutSec)
    $lockedJsonl    = $null
    $rolloutState   = @{}
    $targetTurnId   = ""
    $gotJsonlReply  = $false
    $turnAborted    = $false
    $losslessReply  = ""
    $modelDurMs     = 0
    $modelTtftMs    = 0
    $lastUiCheckAt  = (Get-Date).AddSeconds(2.5)
    $stableTicks    = 0
    $prevStream     = ""
    $uiaReply       = ""
    $uiaConfirmed   = $false
    $finalSnap      = $null

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 35

        $lengths = if (-not $lockedJsonl) { Get-CodexRolloutBaseline } else { @{} }
        $paths = if ($lockedJsonl) { @($lockedJsonl) } else { $lengths.Keys }
        foreach ($fPath in $paths) {
            if (-not $rolloutState.ContainsKey($fPath)) {
                $rolloutState[$fPath] = @{
                    offset = if ($rolloutBaseline.ContainsKey($fPath)) { $rolloutBaseline[$fPath] } else { [long]0 }
                    active_turn = ""
                    matched_turn = ""
                    awaiting_turn = $false
                }
            }
            $state = $rolloutState[$fPath]
            if (-not $lockedJsonl -and $lengths[$fPath] -le $state.offset) { continue }
            $chunk = Read-SharedTailText $fPath $state.offset
            $state.offset = $chunk.new_offset
            if (-not $chunk.text) { continue }

            foreach ($ln in ($chunk.text -split "`r?`n")) {
                if (-not $ln.Trim()) { continue }
                try { $evt = $ln | ConvertFrom-Json } catch { continue }
                $pl = $evt.payload
                if ($evt.type -eq "event_msg" -and $pl.type -in @("task_started", "turn_started")) {
                    $state.active_turn = [string]$pl.turn_id
                    if ($state.awaiting_turn -and $state.active_turn) {
                        $state.matched_turn = $state.active_turn
                        $state.awaiting_turn = $false
                    }
                } elseif (-not $state.matched_turn) {
                    $userText = if ($evt.type -eq "event_msg" -and $pl.type -eq "user_message") {
                        @([string]$pl.message)
                    } elseif ($evt.type -eq "response_item" -and $pl.type -eq "message" -and $pl.role -eq "user") {
                        $pl.content | Where-Object { $_.type -eq "input_text" } | ForEach-Object { [string]$_.text }
                    }
                    foreach ($text in $userText) {
                        if ($text.Contains($promptAnchor)) {
                            $state.matched_turn = if ($pl.turn_id) { [string]$pl.turn_id } else { $state.active_turn }
                            $state.awaiting_turn = -not $state.matched_turn
                            break
                        }
                    }
                }
                if ($state.matched_turn) {
                    $lockedJsonl = $fPath
                    $targetTurnId = $state.matched_turn
                }

                if ($evt.type -eq "event_msg" -and $pl.type -in @("task_complete", "turn_aborted")) {
                    if ($pl.turn_id -ceq $state.active_turn) { $state.active_turn = "" }
                    if ($targetTurnId -and $fPath -eq $lockedJsonl -and $pl.turn_id -ceq $targetTurnId) {
                        if ($pl.type -eq "turn_aborted") {
                            $turnAborted = $true
                        } else {
                            $losslessReply = [string]$pl.last_agent_message
                            $modelDurMs = [int]$pl.duration_ms
                            $modelTtftMs = [int]$pl.time_to_first_token_ms
                            $gotJsonlReply = $true
                        }
                        break
                    }
                }
            }
            if ($lockedJsonl) { break }
        }
        if ($gotJsonlReply -or $turnAborted) { break }

        if (-not $lockedJsonl -and (Get-Date) -ge $lastUiCheckAt) {
            $lastUiCheckAt = (Get-Date).AddMilliseconds(500)
            $finalSnap = Build-Snapshot $targetWin $false
            $curStream = [string]$finalSnap._full_stream
            $tail = Get-DispatchUiTail $curStream $promptAnchor
            $assistant = [regex]::Match($tail, '(?im)^[^\S\r\n]*(?:ChatGPT 说：|ChatGPT said:|Codex 说：|Codex said:|最新一条回复|Latest reply)[^\S\r\n]*\r?$')
            $uiaReply = ""
            if ($assistant.Success -and $curStream -cne $baselineStream -and -not $finalSnap.is_generating) {
                $replyTail = $tail.Substring($assistant.Index + $assistant.Length)
                $summary = [regex]::Match($replyTail, $script:summaryLine)
                if ($summary.Success) { $uiaReply = $replyTail.Substring($summary.Index).Trim() }
            }
            $stableTicks = if ($uiaReply -and $curStream -ceq $prevStream) { $stableTicks + 1 } else { 0 }
            $uiaConfirmed = $stableTicks -ge 2
            if ($uiaConfirmed) { break }
            $prevStream = $curStream
        }
    }

    $totalElapsedMs = $sw.ElapsedMilliseconds

    if ($turnAborted) {
        Write-JsonResult @{
            ok               = $false
            error_code       = "TURN_ABORTED"
            dispatch_id      = $DispatchId
            turn_id          = $targetTurnId
            total_elapsed_ms = $totalElapsedMs
            message          = "目标会话本轮生成被中止 (turn_aborted)。"
        }
        return
    }

    if ($gotJsonlReply) {
        try { $finalSnap = Build-Snapshot $targetWin $false } catch { $finalSnap = $snapBefore }
        $overheadMs = [Math]::Max(0, $totalElapsedMs - $injectDoneMs - $modelDurMs)
        Write-JsonResult @{
            ok                 = $true
            timed_out          = $false
            channel            = "codex_jsonl_lossless_35ms"
            dispatch_id        = $DispatchId
            turn_id            = $targetTurnId
            ui_inject_ms       = $injectDoneMs
            model_ttft_ms      = $modelTtftMs
            model_duration_ms  = $modelDurMs
            pickup_overhead_ms = $overheadMs
            total_elapsed_ms   = $totalElapsedMs
            active_session     = $finalSnap.active_session
            title_authoritative = $finalSnap.title_authoritative
            hwnd               = $targetWin.Hwnd
            raw_reply          = $losslessReply
            message            = ("指令极速无损回收成功 [通道=JSONL直连 | 投递={0}ms | 模型TTFT={1}ms | 模型生成={2}ms | 回收延迟={3}ms | 端到端总计={4}ms]" -f $injectDoneMs, $modelTtftMs, $modelDurMs, $overheadMs, $totalElapsedMs)
        }
        return
    }

    if ($null -eq $finalSnap) { $finalSnap = $snapBefore }
    $timedOut = -not $uiaConfirmed

    Write-JsonResult @{
        ok               = (-not $timedOut)
        timed_out        = $timedOut
        channel          = "uia_fallback"
        dispatch_id      = $DispatchId
        ui_inject_ms     = $injectDoneMs
        total_elapsed_ms = $totalElapsedMs
        active_session   = $finalSnap.active_session
        title_authoritative = $finalSnap.title_authoritative
        hwnd             = $targetWin.Hwnd
        raw_reply        = if ($uiaConfirmed) { $uiaReply } else { "" }
        message          = if ($timedOut) { "等待 $TimeoutSec 秒后仍未确认本轮回复，可稍后查收。" } else { "指令已通过 UIA 通道收回回复 (总耗时 ${totalElapsedMs}ms)。" }
    }
    return
}
