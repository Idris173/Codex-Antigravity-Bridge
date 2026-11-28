using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Automation;

public class WindowScanData {
    public AutomationElement PromptElement;
    public bool IsGenerating;
    public double SidebarBoundaryX;
    public double MainRightBoundaryX;
    public string HeaderTitle = "";
    public bool TitleAuthoritative;
    public string SelectedSidebar = "";
    public List<string> SidebarSessions = new List<string>();
    public List<string> MainTexts = new List<string>();
    public int ScannedCount;
}

public static class BridgeFastUia {
    [StructLayout(LayoutKind.Sequential)]
    struct LASTINPUTINFO {
        public uint cbSize;
        public uint dwTime;
    }

    [DllImport("user32.dll")]
    static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [StructLayout(LayoutKind.Sequential)]
    public struct WINRECT {
        public int Left, Top, Right, Bottom;
    }

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out WINRECT lpRect);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsWindow(IntPtr hWnd);

    private const int SW_SHOWNOACTIVATE = 4;

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    public static void RestoreWithoutActivation(IntPtr hWnd) {
        if (IsIconic(hWnd)) ShowWindow(hWnd, SW_SHOWNOACTIVATE);
    }

    public static bool HasComposerFocus(AutomationElement composer) {
        try {
            for (AutomationElement current = AutomationElement.FocusedElement; current != null;
                 current = TreeWalker.RawViewWalker.GetParent(current)) {
                if (Automation.Compare(current, composer)) return true;
            }
        } catch (ElementNotAvailableException) { }
        return false;
    }

    public static List<IntPtr> FindChatGptWindowHandlesFast() {
        var targetPids = new HashSet<uint>();
        foreach (string pName in new[] { "ChatGPT", "Codex", "codex" }) {
            try {
                var procs = System.Diagnostics.Process.GetProcessesByName(pName);
                foreach (var p in procs) {
                    targetPids.Add((uint)p.Id);
                }
            } catch { }
        }

        var hwnds = new List<IntPtr>();
        if (targetPids.Count == 0) return hwnds;

        EnumWindows((hWnd, lParam) => {
            if (!IsWindowVisible(hWnd)) return true;
            uint pid = 0;
            GetWindowThreadProcessId(hWnd, out pid);
            if (targetPids.Contains(pid)) {
                bool iconic = IsIconic(hWnd);
                WINRECT r;
                if (iconic || (GetWindowRect(hWnd, out r) && (r.Right - r.Left) >= 180 && (r.Bottom - r.Top) >= 180)) {
                    hwnds.Add(hWnd);
                }
            }
            return true;
        }, IntPtr.Zero);

        return hwnds;
    }

    public static uint GetIdleMilliseconds() {
        LASTINPUTINFO lii = new LASTINPUTINFO();
        lii.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
        if (!GetLastInputInfo(ref lii)) return 999999;
        uint tick = (uint)Environment.TickCount;
        return tick >= lii.dwTime ? (tick - lii.dwTime) : 0;
    }

    public static bool ActivateWindowReliable(IntPtr targetHwnd) {
        if (GetForegroundWindow() == targetHwnd) return true;
        IntPtr fg = GetForegroundWindow();
        uint dummy = 0;
        uint fgThread = fg != IntPtr.Zero ? GetWindowThreadProcessId(fg, out dummy) : 0;
        uint curThread = GetCurrentThreadId();
        bool attached = false;
        try {
            if (fgThread != 0 && fgThread != curThread) {
                attached = AttachThreadInput(curThread, fgThread, true);
            }
            keybd_event(0x10, 0, 0x0002, UIntPtr.Zero);
            BringWindowToTop(targetHwnd);
            SetForegroundWindow(targetHwnd);
        } finally {
            if (attached) {
                AttachThreadInput(curThread, fgThread, false);
            }
        }
        return GetForegroundWindow() == targetHwnd;
    }

    private static readonly HashSet<string> NoiseSet = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
        "ChatGPT", "New chat", "新建聊天", "新聊天", "Search chats", "搜索聊天", "搜索", "Search",
        "Library", "库", "Explore GPTs", "探索 GPT", "Sidebar", "侧边栏",
        "Open sidebar", "Close sidebar", "Hide sidebar", "Show sidebar", "打开侧边栏", "关闭侧边栏", "隐藏侧边栏", "显示侧边栏", "展开侧边栏", "收起侧边栏",
        "Share", "共享", "分享", "Temporary Chat", "临时聊天",
        "Attach files", "Add files and more", "附加文件", "添加文件等内容", "Voice", "Dictate", "语音", "听写", "Send", "发送",
        "Minimize", "Maximize", "Close", "最小化", "最大化", "关闭", "Restore",
        "Home", "Automations", "Plugins", "Explore", "Code review", "Sites", "Usage",
        "首页", "资料空间", "定时任务", "插件", "探索", "代码审查", "站点", "用量",
        "Open profile menu", "Show more", "打开个人资料菜单", "展开显示",
        "Skip to content", "Back", "Forward", "App navigation", "Focus current chat",
        "跳转到内容", "返回", "前进", "应用导航", "聚焦当前聊天",
        "Pinned", "Pin chat", "Archive chat", "Toggle summary pane", "New tab", "Enter full view", "Hide tabs",
        "置顶", "置顶聊天", "归档聊天", "切换摘要面板固定状态", "新标签页", "进入完整视图", "隐藏标签页",
        "Rate response", "Read aloud", "More actions", "Copy message", "Change permissions", "User messages",
        "评价回复", "朗读", "更多操作", "复制消息", "更改权限", "用户消息",
        "更改 的标记", "你的 Dot", "Your Dot", "项目", "最近", "杂项"
    };

    private static bool IsNoise(string s) {
        if (string.IsNullOrWhiteSpace(s)) return true;
        string t = s.Trim();
        if (t.Length <= 1) return true;
        if (NoiseSet.Contains(t)) return true;
        if (t.StartsWith("跳转到用户消息", StringComparison.Ordinal) ||
            t.StartsWith("Jump to user message", StringComparison.OrdinalIgnoreCase) ||
            t.StartsWith("切换模式", StringComparison.Ordinal) ||
            t.StartsWith("Switch mode", StringComparison.OrdinalIgnoreCase) ||
            t.Contains("中开始新聊天") ||
            t.Contains("中创建新聊天") ||
            t.StartsWith("Start new chat in ", StringComparison.OrdinalIgnoreCase) ||
            t.StartsWith("Create new chat in ", StringComparison.OrdinalIgnoreCase) ||
            t.StartsWith("New chat in ", StringComparison.OrdinalIgnoreCase) ||
            t.EndsWith("侧边栏选项", StringComparison.Ordinal) ||
            t.EndsWith("的选项", StringComparison.Ordinal) ||
            t.EndsWith("的项目操作", StringComparison.Ordinal) ||
            t.EndsWith(" options", StringComparison.OrdinalIgnoreCase) ||
            t.EndsWith(" project actions", StringComparison.OrdinalIgnoreCase) ||
            (t.StartsWith("关闭", StringComparison.Ordinal) && t.EndsWith("标签页", StringComparison.Ordinal)) ||
            (t.StartsWith("Close ", StringComparison.OrdinalIgnoreCase) && t.EndsWith(" tab", StringComparison.OrdinalIgnoreCase))) {
            return true;
        }
        return false;
    }

    private static bool IsStopButtonName(string name) {
        if (string.IsNullOrWhiteSpace(name)) return false;
        string t = name.Trim();
        return t.Equals("停止", StringComparison.OrdinalIgnoreCase) ||
               t.Equals("停止生成", StringComparison.OrdinalIgnoreCase) ||
               t.Equals("停止回应", StringComparison.OrdinalIgnoreCase) ||
               t.Equals("Stop", StringComparison.OrdinalIgnoreCase) ||
               t.Equals("Stop streaming", StringComparison.OrdinalIgnoreCase) ||
               t.Equals("Stop generating", StringComparison.OrdinalIgnoreCase);
    }

    private static readonly Condition SidebarControl = new OrCondition(
        new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button),
        new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Hyperlink));

    private static bool IsSidebarCandidate(Rect r, Rect winRect, double sidebarX, bool offscreen, bool enabled) {
        return !offscreen && enabled && r.Width > 35 && r.Height > 0 &&
            r.Left >= winRect.Left && r.Right <= sidebarX + 20 &&
            r.Top > winRect.Top + 70 && r.Bottom <= winRect.Bottom;
    }

    private static bool IsSelected(AutomationElement el) {
        return object.Equals(el.GetCachedPropertyValue(SelectionItemPattern.IsSelectedProperty), true);
    }

    public static WindowScanData ScanWindowFast(AutomationElement win, bool includeSidebar) {
        var result = new WindowScanData();
        Rect winRect = win.Current.BoundingRectangle;

        var cr = new CacheRequest();
        cr.Add(AutomationElement.ControlTypeProperty);
        cr.Add(AutomationElement.NameProperty);
        cr.Add(AutomationElement.BoundingRectangleProperty);
        cr.Add(AutomationElement.IsOffscreenProperty);
        cr.Add(AutomationElement.IsKeyboardFocusableProperty);
        cr.Add(AutomationElement.IsEnabledProperty);
        cr.Add(SelectionItemPattern.IsSelectedProperty);
        cr.TreeScope = TreeScope.Element;

        Condition cond = new OrCondition(
            SidebarControl,
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Document),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Text),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Header),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.TabItem)
        );

        AutomationElementCollection elements;
        using (cr.Activate()) {
            elements = win.FindAll(TreeScope.Descendants, cond);
            if (elements.Count < 8) {
                System.Threading.Thread.Sleep(300);
                elements = win.FindAll(TreeScope.Descendants, cond);
            }
        }

        result.ScannedCount = elements.Count;
        AutomationElement promptElem = null;
        double promptTopBest = -1;
        double minComposerY = winRect.Top + winRect.Height * 0.40;

        // Pass 1 in pure cached memory: locate composer box
        for (int i = 0; i < elements.Count; i++) {
            AutomationElement el = elements[i];
            try {
                if (el.Cached.IsOffscreen) continue;
                ControlType ct = el.Cached.ControlType;
                if (ct == ControlType.Edit || ct == ControlType.Document) {
                    Rect r = el.Cached.BoundingRectangle;
                    if (el.Cached.IsKeyboardFocusable && r.Height > 18 && r.Top > minComposerY) {
                        if (promptElem == null || r.Top > promptTopBest) {
                            promptElem = el;
                            promptTopBest = r.Top;
                        }
                    }
                }
            } catch { }
        }

        result.PromptElement = promptElem;
        Rect promptRect = promptElem != null ? promptElem.Cached.BoundingRectangle : Rect.Empty;

        double sidebarX = (promptElem != null && (promptRect.Left - winRect.Left) > 160)
            ? (promptRect.Left - 20)
            : (winRect.Left + winRect.Width * 0.25);
        double mainRightX = (promptElem != null) ? (promptRect.Right + 140) : winRect.Right;
        double headerMaxY = winRect.Top + Math.Min(54.0, winRect.Height * 0.08);
        double mainMaxY = (promptElem != null) ? (promptRect.Top + 4) : winRect.Bottom;

        result.SidebarBoundaryX = sidebarX;
        result.MainRightBoundaryX = mainRightX;

        var seenSidebar = new HashSet<string>(StringComparer.Ordinal);
        string selectedTab = "";

        // Pass 2 in pure cached memory: zero IPC overhead!
        for (int i = 0; i < elements.Count; i++) {
            AutomationElement el = elements[i];
            try {
                bool off = el.Cached.IsOffscreen;
                ControlType ct = el.Cached.ControlType;
                string rawName = el.Cached.Name ?? "";
                Rect r = el.Cached.BoundingRectangle;

                if (off) continue;

                string trimmed = rawName.Trim();
                if ((ct == ControlType.Button || ct == ControlType.Hyperlink) && trimmed.Length <= 90 &&
                    IsSidebarCandidate(r, winRect, sidebarX, off, el.Cached.IsEnabled) && !IsNoise(trimmed)) {
                    if (includeSidebar && seenSidebar.Add(trimmed)) result.SidebarSessions.Add(trimmed);
                    if (result.SelectedSidebar.Length == 0 && IsSelected(el)) result.SelectedSidebar = trimmed;
                }

                if (ct == ControlType.Button) {
                    if (r.Left > sidebarX && r.Right <= (mainRightX + 60) && IsStopButtonName(rawName)) {
                        result.IsGenerating = true;
                    }
                }

                if (!r.IsEmpty && r.Top >= winRect.Top && r.Bottom <= headerMaxY && !IsNoise(trimmed)) {
                    if (ct == ControlType.TabItem && IsSelected(el)) {
                        selectedTab = trimmed;
                    } else if (result.HeaderTitle.Length == 0 && r.Left > sidebarX && r.Right <= mainRightX &&
                        (ct == ControlType.Text || ct == ControlType.Header)) {
                        result.HeaderTitle = trimmed;
                        result.TitleAuthoritative = true;
                    }
                }

                if (ct == ControlType.Text && r.Left > (sidebarX - 5) && r.Right <= (mainRightX + 40) &&
                    r.Top >= (winRect.Top + 55) && r.Bottom <= mainMaxY) {
                    string t = rawName.Trim();
                    if (t.Length > 0 && !NoiseSet.Contains(t)) {
                        int mCount = result.MainTexts.Count;
                        if (mCount == 0 || !string.Equals(result.MainTexts[mCount - 1], t, StringComparison.Ordinal)) {
                            result.MainTexts.Add(t);
                        }
                    }
                }
            } catch { }
        }

        if (selectedTab.Length > 0 || result.SelectedSidebar.Length > 0) {
            result.HeaderTitle = selectedTab.Length > 0 ? selectedTab : result.SelectedSidebar;
            result.TitleAuthoritative = true;
        }
        return result;
    }

    public static bool TryExpandSidebar(AutomationElement win) {
        return InvokeNavigation(win, null, false, "打开侧边栏", "展开侧边栏", "显示侧边栏",
            "Open sidebar", "Expand sidebar", "Show sidebar") != null;
    }

    public static string InvokeSidebarItem(AutomationElement win, double sidebarX, bool projectAction, params string[] names) {
        return InvokeNavigation(win, sidebarX, projectAction, names);
    }

    private static bool IsProjectActionCandidate(Rect action, Rect row, Rect window, double sidebarX, bool enabled,
                                                 string name, string[] names) {
        // Chromium reports unhovered project actions as offscreen even though their parent row is visible.
        return enabled && action.Width > 0 && action.Height > 0 &&
            action.Left >= window.Left && action.Right <= Math.Min(window.Right, Math.Max(sidebarX + 20, row.Right + 20)) &&
            action.Top >= Math.Max(window.Top, row.Top - 8) && action.Bottom <= Math.Min(window.Bottom, row.Bottom + 8) &&
            Array.Exists(names, candidate => string.Equals(name, candidate, StringComparison.OrdinalIgnoreCase));
    }

    public static string InvokeProjectNewChat(AutomationElement win, double sidebarX, string projectName, params string[] names) {
        if (string.IsNullOrWhiteSpace(projectName)) return null;

        var cache = new CacheRequest();
        cache.Add(AutomationElement.NameProperty);
        cache.Add(AutomationElement.BoundingRectangleProperty);
        cache.Add(AutomationElement.IsOffscreenProperty);
        cache.Add(AutomationElement.IsEnabledProperty);
        cache.TreeScope = TreeScope.Element;

        Rect window = win.Current.BoundingRectangle;
        InvokePattern target = null;
        string targetName = null;
        using (cache.Activate()) {
            foreach (AutomationElement row in win.FindAll(TreeScope.Descendants, SidebarControl)) {
                try {
                    if (!string.Equals((row.Cached.Name ?? "").Trim(), projectName, StringComparison.OrdinalIgnoreCase)) continue;
                    Rect rowRect = row.Cached.BoundingRectangle;
                    if (!IsSidebarCandidate(rowRect, window, sidebarX, row.Cached.IsOffscreen, row.Cached.IsEnabled)) continue;

                    foreach (AutomationElement button in row.FindAll(TreeScope.Descendants, SidebarControl)) {
                        try {
                            string name = (button.Cached.Name ?? "").Trim();
                            if (!IsProjectActionCandidate(button.Cached.BoundingRectangle, rowRect, window, sidebarX,
                                                          button.Cached.IsEnabled, name, names)) continue;
                            object pattern;
                            if (!button.TryGetCurrentPattern(InvokePattern.Pattern, out pattern)) continue;
                            if (target != null) return null;
                            target = (InvokePattern)pattern;
                            targetName = name;
                        } catch (ElementNotAvailableException) { }
                    }
                } catch (ElementNotAvailableException) { }
            }
        }

        if (target == null) return null;
        target.Invoke();
        return targetName;
    }

    private static string InvokeNavigation(AutomationElement win, double? sidebarX, bool projectAction, params string[] names) {
        var cr = new CacheRequest();
        cr.Add(AutomationElement.NameProperty);
        cr.Add(AutomationElement.BoundingRectangleProperty);
        cr.Add(AutomationElement.IsOffscreenProperty);
        cr.Add(AutomationElement.IsEnabledProperty);
        cr.TreeScope = TreeScope.Element;

        AutomationElementCollection candidates;
        using (cr.Activate()) {
            candidates = win.FindAll(TreeScope.Descendants, SidebarControl);
        }

        Rect winRect = win.Current.BoundingRectangle;
        InvokePattern target = null;
        string targetName = null;
        InvokePattern partial = null;
        string partialName = null;
        bool ambiguousPartial = false;
        int best = names.Length;
        foreach (AutomationElement el in candidates) {
            try {
                bool offscreen = el.Cached.IsOffscreen;
                bool enabled = el.Cached.IsEnabled;
                if (offscreen || !enabled) continue;
                if (sidebarX.HasValue && !IsSidebarCandidate(el.Cached.BoundingRectangle, winRect, sidebarX.Value, offscreen, enabled)) continue;

                string name = (el.Cached.Name ?? "").Trim();
                if (sidebarX.HasValue && !projectAction && (name.Length > 90 || IsNoise(name))) continue;
                int match = Array.FindIndex(names, value => string.Equals(name, value, StringComparison.OrdinalIgnoreCase));
                bool partialMatch = match < 0 && sidebarX.HasValue && !projectAction && names.Length > 0 &&
                    !string.IsNullOrWhiteSpace(names[0]) && name.IndexOf(names[0], StringComparison.OrdinalIgnoreCase) >= 0;
                object pattern;
                if ((partialMatch || (match >= 0 && match < best)) && el.TryGetCurrentPattern(InvokePattern.Pattern, out pattern)) {
                    if (partialMatch) {
                        ambiguousPartial = partial != null;
                        partial = (InvokePattern)pattern;
                        partialName = name;
                        continue;
                    }
                    best = match;
                    target = (InvokePattern)pattern;
                    targetName = name;
                }
            } catch (ElementNotAvailableException) { }
        }

        if (target == null && !ambiguousPartial) {
            target = partial;
            targetName = partialName;
        }
        if (target == null) return null;
        target.Invoke();
        return targetName;
    }

    public static bool TryClickComposerSendButton(AutomationElement win, double sidebarX, double mainRightX, double minTopY) {
        var cr = new CacheRequest();
        cr.Add(AutomationElement.NameProperty);
        cr.Add(AutomationElement.BoundingRectangleProperty);
        cr.Add(AutomationElement.IsOffscreenProperty);
        cr.Add(AutomationElement.IsEnabledProperty);
        cr.TreeScope = TreeScope.Element;

        Condition btnCond = new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button);
        AutomationElementCollection buttons;
        using (cr.Activate()) {
            buttons = win.FindAll(TreeScope.Descendants, btnCond);
        }

        for (int i = 0; i < buttons.Count; i++) {
            AutomationElement b = buttons[i];
            InvokePattern send = null;
            try {
                if (b.Cached.IsOffscreen || !b.Cached.IsEnabled) continue;
                Rect r = b.Cached.BoundingRectangle;
                if (r.Left > sidebarX && r.Right <= (mainRightX + 80) && r.Top >= (minTopY - 30)) {
                    string n = (b.Cached.Name ?? "").Trim();
                    if (n.Equals("发送", StringComparison.OrdinalIgnoreCase) ||
                        n.Equals("发送消息", StringComparison.OrdinalIgnoreCase) ||
                        n.Equals("发送提示", StringComparison.OrdinalIgnoreCase) ||
                        n.Equals("提交", StringComparison.OrdinalIgnoreCase) ||
                        n.Equals("Send", StringComparison.OrdinalIgnoreCase) ||
                        n.Equals("Send message", StringComparison.OrdinalIgnoreCase) ||
                        n.Equals("Send prompt", StringComparison.OrdinalIgnoreCase) ||
                        n.Equals("Submit", StringComparison.OrdinalIgnoreCase)) {
                        object patObj;
                        if (b.TryGetCurrentPattern(InvokePattern.Pattern, out patObj)) {
                            send = (InvokePattern)patObj;
                        }
                    }
                }
            } catch (ElementNotAvailableException) { }
            if (send != null) {
                send.Invoke();
                return true;
            }
        }
        return false;
    }
}
