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
        "评价回复", "朗读", "更多操作", "复制消息", "更改权限", "用户消息"
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
        cr.TreeScope = TreeScope.Element;

        Condition cond = new OrCondition(
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Document),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Text),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Header),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Hyperlink)
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
        double headerMaxY = winRect.Top + winRect.Height * 0.10;
        double mainMaxY = (promptElem != null) ? (promptRect.Top + 4) : winRect.Bottom;

        result.SidebarBoundaryX = sidebarX;
        result.MainRightBoundaryX = mainRightX;

        var seenSidebar = new HashSet<string>(StringComparer.Ordinal);

        // Pass 2 in pure cached memory: zero IPC overhead!
        for (int i = 0; i < elements.Count; i++) {
            AutomationElement el = elements[i];
            try {
                bool off = el.Cached.IsOffscreen;
                ControlType ct = el.Cached.ControlType;
                string rawName = el.Cached.Name ?? "";
                Rect r = el.Cached.BoundingRectangle;

                if (includeSidebar && r.Width > 35 && r.Right <= (sidebarX + 20) && r.Top > (winRect.Top + 70)) {
                    if (ct == ControlType.Button || ct == ControlType.Hyperlink) {
                        string trimmed = rawName.Trim();
                        if (trimmed.Length <= 90 && !IsNoise(trimmed)) {
                            if (seenSidebar.Add(trimmed)) {
                                result.SidebarSessions.Add(trimmed);
                            }
                        }
                    }
                }

                if (off) continue;

                if (ct == ControlType.Button) {
                    if (r.Left > sidebarX && r.Right <= (mainRightX + 60) && IsStopButtonName(rawName)) {
                        result.IsGenerating = true;
                    }
                }

                if (result.HeaderTitle.Length == 0 && r.Left > sidebarX && r.Right <= mainRightX &&
                    r.Top >= (winRect.Top + 4) && r.Bottom <= headerMaxY) {
                    if (ct == ControlType.Text || ct == ControlType.Header || ct == ControlType.Button) {
                        string trimmed = rawName.Trim();
                        if (!IsNoise(trimmed)) {
                            result.HeaderTitle = trimmed;
                        }
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

        return result;
    }

    public static string InvokeButtonFast(AutomationElement win, string exactPrimary, string exactSecondary, string containsFallback) {
        var cr = new CacheRequest();
        cr.Add(AutomationElement.NameProperty);
        cr.TreeScope = TreeScope.Element;

        Condition btnCond = new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button);
        AutomationElementCollection buttons;
        using (cr.Activate()) {
            buttons = win.FindAll(TreeScope.Descendants, btnCond);
        }

        AutomationElement matchPrimary = null;
        AutomationElement matchSecondary = null;
        AutomationElement matchFallback = null;

        for (int i = 0; i < buttons.Count; i++) {
            AutomationElement b = buttons[i];
            try {
                string n = (b.Cached.Name ?? "").Trim();
                if (n.Length == 0) continue;
                if (!string.IsNullOrEmpty(exactPrimary) && string.Equals(n, exactPrimary, StringComparison.OrdinalIgnoreCase)) {
                    matchPrimary = b;
                    break;
                }
                if (matchSecondary == null && !string.IsNullOrEmpty(exactSecondary) && string.Equals(n, exactSecondary, StringComparison.OrdinalIgnoreCase)) {
                    matchSecondary = b;
                }
                if (matchFallback == null && !string.IsNullOrEmpty(containsFallback) && n.IndexOf(containsFallback, StringComparison.OrdinalIgnoreCase) >= 0) {
                    matchFallback = b;
                }
            } catch { }
        }

        AutomationElement target = matchPrimary ?? matchSecondary ?? matchFallback;
        if (target == null) return null;

        object patObj;
        if (target.TryGetCurrentPattern(InvokePattern.Pattern, out patObj)) {
            ((InvokePattern)patObj).Invoke();
            return target.Cached.Name ?? "";
        }
        return null;
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
                            ((InvokePattern)patObj).Invoke();
                            return true;
                        }
                    }
                }
            } catch { }
        }
        return false;
    }
}
