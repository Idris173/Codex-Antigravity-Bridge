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
    [int]$TimeoutSec = 180
)

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

function Resolve-TargetWindows([int]$FastHwnd) {
    if ($FastHwnd -ne 0) {
        try {
            $el = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$FastHwnd)
            if ($null -ne $el) {
                $pidVal = $el.Current.ProcessId
                $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
                if ($proc -and $proc.ProcessName -match '^(ChatGPT|Codex)$') {
                    $isMin = [BridgeFastUia]::IsIconic([IntPtr]$FastHwnd)
                    return @([PSCustomObject]@{
                        Element      = $el
                        Hwnd         = $FastHwnd
                        Pid          = $pidVal
                        ProcessStart = $proc.StartTime.ToFileTimeUtc()
                        ProcessName  = $proc.ProcessName
                        WindowTitle  = [string]$el.Current.Name
                        IsMinimized  = $isMin
                    })
                }
            }
        } catch {}
    }

    $hwndList = [BridgeFastUia]::FindChatGptWindowHandlesFast()
    $results = @()
    foreach ($h in $hwndList) {
        try {
            $hwndInt = $h.ToInt32()
            $el = [System.Windows.Automation.AutomationElement]::FromHandle($h)
            if ($null -eq $el) { continue }
            $pidVal = $el.Current.ProcessId
            $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
            $pStart = if ($proc) { $proc.StartTime.ToFileTimeUtc() } else { 0 }
            $isMin  = [BridgeFastUia]::IsIconic($h)
            $results += [PSCustomObject]@{
                Element      = $el
                Hwnd         = $hwndInt
                Pid          = $pidVal
                ProcessStart = $pStart
                ProcessName  = if ($proc) { $proc.ProcessName } else { "ChatGPT" }
                WindowTitle  = [string]$el.Current.Name
                IsMinimized  = $isMin
            }
        } catch { continue }
    }
    return $results
}

function Build-Snapshot($WinInfo, [bool]$IncludeSidebar) {
    if ($WinInfo.IsMinimized) {
        return @{
            hwnd               = $WinInfo.Hwnd
            pid                = $WinInfo.Pid
            process_start      = $WinInfo.ProcessStart
            process_name       = $WinInfo.ProcessName
            window_title       = $WinInfo.WindowTitle
            is_minimized       = $true
            active_session     = $WinInfo.WindowTitle
            sidebar_sessions   = @()
            recent_messages    = @()
            last_message       = ""
            full_stream_length = 0
            is_generating      = $false
            has_prompt_box     = $false
            fingerprint        = "MINIMIZED:$($WinInfo.Hwnd)"
        }
    }

    $scan = [BridgeFastUia]::ScanWindowFast($WinInfo.Element, $IncludeSidebar)
    $mainTexts = $scan.MainTexts

    $activeTitle = ""
    $cleanWinTitle = ($WinInfo.WindowTitle -replace '\s*[-—|]\s*ChatGPT.*$', '').Trim()
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
    $lastSummaryIdx = $joinedStream.LastIndexOf("[SUMMARY]")
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
        window_title       = $WinInfo.WindowTitle
        is_minimized       = $false
        active_session     = $activeTitle
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

# --- Entry Dispatch ---
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$fastLookupHwnd = if ($Action -in @("dispatch", "read_latest") -and $TargetHwnd -ne 0) { $TargetHwnd } else { 0 }
$windows = @(Resolve-TargetWindows $fastLookupHwnd)

if ($windows.Count -eq 0) {
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
        exit 0
    }
    Write-JsonResult @{
        ok         = $false
        error_code = "APP_NOT_RUNNING"
        elapsed_ms = $sw.ElapsedMilliseconds
        message    = "未检测到正在运行的 ChatGPT 桌面应用窗口，请先打开 ChatGPT 应用。"
        windows    = @()
    }
    exit 0
}

if ($Action -eq "new_project_session") {
    $clicked = [BridgeFastUia]::InvokeButtonFast($windows[0].Element, "在 $ProjectName 中开始新聊天", "Start new chat in $ProjectName", "$ProjectName 中开始新聊天")
    if ($null -eq $clicked) {
        $clicked = [BridgeFastUia]::InvokeButtonFast($windows[0].Element, "在 $ProjectName 中创建新聊天", "New chat in $ProjectName", "chat in $ProjectName")
    }
    if ($null -ne $clicked) { Start-Sleep -Milliseconds 500 }
    $snap = Build-Snapshot $windows[0] $false
    Strip-InternalFields $snap
    Write-JsonResult @{
        ok         = ($null -ne $clicked)
        button     = $clicked
        elapsed_ms = $sw.ElapsedMilliseconds
        snapshot   = $snap
        message    = if ($null -ne $clicked) { "已在 '$ProjectName' 下开启新会话。" } else { "未找到工程 '$ProjectName' 的新建聊天按钮。" }
    }
    exit 0
}

if ($Action -eq "switch_session") {
    $clicked = [BridgeFastUia]::InvokeButtonFast($windows[0].Element, $SessionName, "", $SessionName)
    if ($null -ne $clicked) { Start-Sleep -Milliseconds 500 }
    $snap = Build-Snapshot $windows[0] $false
    Strip-InternalFields $snap
    Write-JsonResult @{
        ok         = ($null -ne $clicked)
        button     = $clicked
        elapsed_ms = $sw.ElapsedMilliseconds
        snapshot   = $snap
        message    = if ($null -ne $clicked) { "已切换到会话 '$clicked'。" } else { "未在侧边栏找到会话 '$SessionName'。" }
    }
    exit 0
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
    exit 0
}

$targetWin = $windows[0]
if ($TargetPid -ne 0 -and $targetWin.Pid -ne $TargetPid) {
    Write-JsonResult @{ ok = $false; error_code = "PID_MISMATCH"; message = "目标窗口进程 PID 已变更，请重新绑定会话。" }
    exit 0
}

$snapBefore = Build-Snapshot $targetWin $false
if ($snapBefore.is_minimized) {
    Write-JsonResult @{ ok = $false; error_code = "WINDOW_MINIMIZED"; message = "ChatGPT 窗口处于最小化状态。" }
    exit 0
}

if ($ExpectedSession -ne "") {
    $exp = $ExpectedSession.Trim()
    $act = [string]$snapBefore.active_session
    $winT = [string]$snapBefore.window_title
    $isInitialPlaceholder = ($exp -in @("新聊天", "New chat", "UE6.0Source", "你想让我们在", "What should we build", "你说：", "You said:", "ChatGPT 说：", "ChatGPT said:", "[SUMMARY]", "[CONSTRAINTS]") -or $exp -match '^\d{1,2}:\d{2}(\s?[AP]M)?$' -or $exp -like "*开始新聊天*" -or $exp -like "*你想让我们在*" -or $exp -like "*What should we*")
    if (-not $isInitialPlaceholder -and $act -notmatch '^\d{1,2}:\d{2}$') {
        if ($act -notlike "*$exp*" -and $exp -notlike "*$act*" -and $winT -notlike "*$exp*") {
            Write-JsonResult @{
                ok             = $false
                error_code     = "SESSION_MISMATCH"
                message        = "防串台拦截：当前激活会话为 '$act'，与锁定的 '$exp' 不一致！"
                active_session = $act
                expected       = $exp
            }
            exit 0
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
    exit 0
}

if ($Action -eq "dispatch") {
    if (-not $PayloadFile -or -not (Test-Path $PayloadFile)) {
        Write-JsonResult @{ ok = $false; error_code = "MISSING_PAYLOAD"; message = "缺少 PayloadFile。" }
        exit 0
    }
    if ($snapBefore.is_generating) {
        Write-JsonResult @{ ok = $false; error_code = "BUSY_GENERATING"; message = "目标会话正在生成回复中，请稍候。" }
        exit 0
    }
    $promptElem = $snapBefore._prompt_elem
    if ($null -eq $promptElem) {
        Write-JsonResult @{ ok = $false; error_code = "PROMPT_BOX_NOT_FOUND"; message = "未定位到底部聊天输入框。" }
        exit 0
    }

    $payloadText    = [System.IO.File]::ReadAllText($PayloadFile, [System.Text.Encoding]::UTF8)
    $baselineStream = [string]$snapBefore._full_stream
    $baselineLen    = $baselineStream.Length
    $sidebarX       = $snapBefore._sidebar_x
    $mainRightX     = $snapBefore._main_right_x

    # Exact turn correlation marker (DispatchId takes precedence over STEP_GOAL)
    $dispatchMarker = if ($DispatchId -ne "") { "[DISPATCH_ID: $DispatchId]" } else { "" }
    $goalMatch      = [regex]::Match($payloadText, '\[STEP_GOAL\]\s*([^\r\n]+)')
    $promptAnchor   = if ($dispatchMarker -ne "") { $dispatchMarker } elseif ($goalMatch.Success) { $goalMatch.Groups[1].Value.Trim() } else { "[DIRECTOR_TASK]" }
    $rolloutBaseline = Get-CodexRolloutBaseline

    # Idle guard when another window is active
    $origFgHwnd = [BridgeFastUia]::GetForegroundWindow()
    if ($origFgHwnd -ne [IntPtr]$targetWin.Hwnd) {
        $waitedMs = 0
        while ([BridgeFastUia]::GetIdleMilliseconds() -lt 1000 -and $waitedMs -lt 5000) {
            Start-Sleep -Milliseconds 150
            $waitedMs += 150
        }
        if ([BridgeFastUia]::GetIdleMilliseconds() -lt 900) {
            Write-JsonResult @{
                ok         = $false
                error_code = "USER_ACTIVE_INPUT"
                message    = "检测到你正在前台高频操作键鼠，为防抢焦点已暂缓投递。"
            }
            exit 0
        }
    }

    $usedSilentValuePattern = $false
    $valPattern = $null
    if ($promptElem.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$valPattern)) {
        try {
            if (-not $valPattern.Current.IsReadOnly) {
                $valPattern.SetValue($payloadText)
                Start-Sleep -Milliseconds 90
                if ([string]$valPattern.Current.Value -ne "") { $usedSilentValuePattern = $true }
            }
        } catch { $usedSilentValuePattern = $false }
    }

    $hadClipboardText = $false
    $savedClipboard   = ""
    try {
        if (-not $usedSilentValuePattern -and [System.Windows.Forms.Clipboard]::ContainsText()) {
            $hadClipboardText = $true
            $savedClipboard = [System.Windows.Forms.Clipboard]::GetText()
        }
    } catch {}

    try {
        if (-not $usedSilentValuePattern) {
            [System.Windows.Forms.Clipboard]::SetText($payloadText)
            [BridgeFastUia]::ActivateWindowReliable([IntPtr]$targetWin.Hwnd) | Out-Null
            Start-Sleep -Milliseconds 70
            try { $promptElem.SetFocus() } catch {}
            Start-Sleep -Milliseconds 60

            if ([BridgeFastUia]::GetForegroundWindow() -ne [IntPtr]$targetWin.Hwnd) {
                Write-JsonResult @{
                    ok         = $false
                    error_code = "FOCUS_BLOCKED"
                    message    = "Windows 阻止了后台切换到 ChatGPT 窗口，已安全中止粘贴。"
                }
                exit 0
            }

            [System.Windows.Forms.SendKeys]::SendWait("^a")
            Start-Sleep -Milliseconds 30
            [System.Windows.Forms.SendKeys]::SendWait("^v")
            Start-Sleep -Milliseconds 120
        }

        $pRect = $promptElem.Current.BoundingRectangle
        $sentViaBtn = [BridgeFastUia]::TryClickComposerSendButton($targetWin.Element, $sidebarX, $mainRightX, $pRect.Top)

        if (-not $sentViaBtn) {
            if ($usedSilentValuePattern) {
                [BridgeFastUia]::ActivateWindowReliable([IntPtr]$targetWin.Hwnd) | Out-Null
                Start-Sleep -Milliseconds 60
                try { $promptElem.SetFocus() } catch {}
                Start-Sleep -Milliseconds 50
            }
            if ([BridgeFastUia]::GetForegroundWindow() -eq [IntPtr]$targetWin.Hwnd) {
                [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
            }
        }
    } finally {
        Start-Sleep -Milliseconds 60
        try {
            if ($hadClipboardText -and $null -ne $savedClipboard) {
                [System.Windows.Forms.Clipboard]::SetText($savedClipboard)
            }
        } catch {}

        if (-not $usedSilentValuePattern -and $origFgHwnd -ne [IntPtr]::Zero -and $origFgHwnd -ne [IntPtr]$targetWin.Hwnd) {
            [BridgeFastUia]::ActivateWindowReliable($origFgHwnd) | Out-Null
        }
    }

    $injectDoneMs = $sw.ElapsedMilliseconds

    # Exact turn-correlated 35ms JSONL pickup + UIA fallback
    $deadline       = (Get-Date).AddSeconds($TimeoutSec)
    $lockedJsonl    = $null
    $jsonlOffset    = [long]0
    $activeTurnId   = ""
    $targetTurnId   = ""
    $gotJsonlReply  = $false
    $turnAborted    = $false
    $losslessReply  = ""
    $modelDurMs     = 0
    $modelTtftMs    = 0
    $lastUiCheckAt  = (Get-Date).AddSeconds(2.5)
    $stableTicks    = 0
    $prevStream     = ""
    $sawChange      = $false
    $finalSnap      = $null

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 35

        if ($null -eq $lockedJsonl) {
            $curMap = Get-CodexRolloutBaseline
            foreach ($kv in $curMap.GetEnumerator()) {
                $fPath = $kv.Key
                $newLen = $kv.Value
                $oldLen = if ($rolloutBaseline.ContainsKey($fPath)) { $rolloutBaseline[$fPath] } else { [long]0 }
                if ($newLen -gt $oldLen) {
                    $delta = Read-SharedTailText $fPath $oldLen
                    if ($delta.text.Contains($promptAnchor)) {
                        $lockedJsonl = $fPath
                        $jsonlOffset = $oldLen
                        break
                    }
                }
            }
        }

        if ($null -ne $lockedJsonl) {
            $chunk = Read-SharedTailText $lockedJsonl $jsonlOffset
            $jsonlOffset = $chunk.new_offset
            if ($chunk.text.Length -gt 0) {
                foreach ($ln in ($chunk.text -split "`r?`n")) {
                    $trimmedLn = $ln.Trim()
                    if ($trimmedLn.Length -eq 0) { continue }
                    try { $evt = $trimmedLn | ConvertFrom-Json } catch { continue }
                    $pl = $evt.payload

                    if ($evt.type -eq "event_msg" -and $pl.type -in @("task_started", "turn_started")) {
                        $activeTurnId = [string]$pl.turn_id
                    } elseif ($evt.type -eq "response_item" -and $pl.type -eq "message" -and $pl.role -eq "user") {
                        foreach ($c in $pl.content) {
                            if ($c.type -eq "input_text" -and ([string]$c.text).Contains($promptAnchor)) {
                                $targetTurnId = if ($pl.turn_id) { [string]$pl.turn_id } else { $activeTurnId }
                                break
                            }
                        }
                    } elseif ($evt.type -eq "event_msg" -and $pl.type -eq "user_message") {
                        if (([string]$pl.message).Contains($promptAnchor)) {
                            $targetTurnId = if ($pl.turn_id) { [string]$pl.turn_id } else { $activeTurnId }
                        }
                    } elseif ($evt.type -eq "event_msg" -and $pl.type -in @("task_complete", "turn_aborted")) {
                        if (-not $targetTurnId -or $pl.turn_id -ceq $targetTurnId) {
                            if ($pl.type -eq "turn_aborted") {
                                $turnAborted = $true
                            } else {
                                $losslessReply = [string]$pl.last_agent_message
                                $modelDurMs    = [int]$pl.duration_ms
                                $modelTtftMs   = [int]$pl.time_to_first_token_ms
                                $gotJsonlReply = $true
                            }
                            break
                        }
                    }
                }
                if ($gotJsonlReply -or $turnAborted) { break }
            }
            continue
        }

        if ((Get-Date) -ge $lastUiCheckAt) {
            $lastUiCheckAt = (Get-Date).AddMilliseconds(500)
            $curSnap   = Build-Snapshot $targetWin $false
            $curStream = [string]$curSnap._full_stream

            if ($curSnap.is_generating -or $curStream.Length -ne $baselineLen -or $curStream -ne $baselineStream) {
                $sawChange = $true
            }
            if ($sawChange -and -not $curSnap.is_generating) {
                $hasAssistantReply = ($curStream -match 'ChatGPT 说：|最新一条回复|\[SUMMARY\]')
                if ($curStream -eq $prevStream -and $curStream.Length -gt 0 -and ($hasAssistantReply -or $stableTicks -ge 4)) {
                    $stableTicks++
                    if ($stableTicks -ge 2) {
                        $finalSnap = $curSnap
                        break
                    }
                } else {
                    if ($curStream -ne $prevStream) { $stableTicks = 0 } else { $stableTicks++ }
                    if ($stableTicks -ge 6) {
                        $finalSnap = $curSnap
                        break
                    }
                }
            } else {
                $stableTicks = 0
            }
            $prevStream = $curStream
            $finalSnap  = $curSnap
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
        exit 0
    }

    if ($gotJsonlReply) {
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
            active_session     = $snapBefore.active_session
            hwnd               = $targetWin.Hwnd
            raw_reply          = $losslessReply
            message            = ("指令极速无损回收成功 [通道=JSONL直连 | 投递={0}ms | 模型TTFT={1}ms | 模型生成={2}ms | 回收延迟={3}ms | 端到端总计={4}ms]" -f $injectDoneMs, $modelTtftMs, $modelDurMs, $overheadMs, $totalElapsedMs)
        }
        exit 0
    }

    if ($null -eq $finalSnap) { $finalSnap = Build-Snapshot $targetWin $false }
    $timedOut = ($finalSnap.is_generating -eq $true)

    Write-JsonResult @{
        ok               = (-not $timedOut)
        timed_out        = $timedOut
        channel          = "uia_fallback"
        dispatch_id      = $DispatchId
        ui_inject_ms     = $injectDoneMs
        total_elapsed_ms = $totalElapsedMs
        active_session   = $finalSnap.active_session
        hwnd             = $targetWin.Hwnd
        raw_reply        = [string]$finalSnap.last_message
        message          = if ($timedOut) { "等待超过 $TimeoutSec 秒（任务仍在后台运行），可稍后查收。" } else { "指令已通过 UIA 通道收回回复 (总耗时 ${totalElapsedMs}ms)。" }
    }
    exit 0
}
