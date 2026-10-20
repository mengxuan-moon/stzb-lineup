// 率土之滨 · 配将推荐 —— 桌面启动器（C#，离线编译成单个 .exe，双击即用）
//
// 为什么要单独写一个 C# 启动器，而不是 PyInstaller 打包 launch.py：
//   1. PyInstaller 的 onefile 产物 8~15MB，冷启动还要解压到 %TEMP%，杀软经常误报；
//   2. 这个 exe 只做三件事——探测健康、必要时调 launch.py 起服务、开浏览器，
//      编译出来 ~15KB，不依赖任何运行时（.NET Framework 4 是 Windows 自带的）。
//   3. python 侧的所有逻辑（幂等、脱离父进程、日志、失败提示）仍然复用 launch.py，
//      这里只是给它加一个“双击图标”，不重复实现业务逻辑。
//
// 编译（Windows 自带 csc，不需要联网、不需要装任何 SDK）：
//   %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:winexe ^
//     /out:"%USERPROFILE%\Desktop\率土配将.exe" /win32icon:stzb_app\tools\stzb.ico ^
//     stzb_app\tools\launcher_gui.cs
//
// 行为：
//   - 8501 已经在跑  -> 只开浏览器（不会叠第二个实例）
//   - 没在跑          -> 以隐藏窗口方式跑 `python -m stzb_app.tools.launch start --no-browser --quiet`
//                        -> 轮询 /_stcore/health 到 200 -> 开浏览器
//   - 起失败          -> 弹窗给出退出码、stderr 片段和日志路径（不会静默失败）
//
// 项目根默认 E:\dsh；如果 exe 旁边有 stzb_root.txt，就用文件里的路径（换机器/换目录不用重新编译）。
using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Threading;
using System.Windows.Forms;

internal static class Program
{
    private const int DefaultPort = 8501;
    private const string DefaultRoot = @"E:\dsh";
    private const string Title = "率土之滨 · 配将推荐";

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = ResolveRoot();
            int port = ResolvePort();

            if (!Healthy(port))
            {
                string pyErr;
                if (!RunLauncher(root, out pyErr))
                {
                    MessageBox.Show(
                        "服务启动失败。\n\n" + pyErr +
                        "\n\n日志： " + Path.Combine(root, @"stzb_app\logs\streamlit.log") +
                        "\n排查： python -m stzb_app.tools.launch status",
                        Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 1;
                }
                // launch.py 返回时健康检查已经通过，这里再给浏览器 1 秒缓冲
                for (int i = 0; i < 20 && !Healthy(port); i++) Thread.Sleep(200);
                if (!Healthy(port))
                {
                    MessageBox.Show(
                        "服务进程起来了，但页面 60 秒内没有响应。\n请稍等几秒再试，或查看日志：\n" +
                        Path.Combine(root, @"stzb_app\logs\streamlit.log"),
                        Title, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return 2;
                }
            }

            try
            {
                Process.Start(new ProcessStartInfo("http://localhost:" + port) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                MessageBox.Show("自动打开浏览器失败，请手动访问 http://localhost:" + port + "\n\n" + ex.Message,
                    Title, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show("启动器异常：\n" + ex.Message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 3;
        }
    }

    // exe 旁边放 stzb_root.txt 可以改项目根（内容就是一行绝对路径）。
    private static string ResolveRoot()
    {
        string dir = Path.GetDirectoryName(Application.ExecutablePath);
        string cfg = Path.Combine(dir, "stzb_root.txt");
        if (File.Exists(cfg))
        {
            string text = File.ReadAllText(cfg).Trim();
            if (text.Length > 0) return text;
        }
        string env = Environment.GetEnvironmentVariable("STZB_ROOT");
        if (!string.IsNullOrEmpty(env)) return env;
        return DefaultRoot;
    }

    private static int ResolvePort()
    {
        int port;
        string env = Environment.GetEnvironmentVariable("STZB_PORT");
        if (!string.IsNullOrEmpty(env) && int.TryParse(env, out port) && port > 0 && port < 65536)
            return port;
        return DefaultPort;
    }

    // 用 launch.py 起服务；先试 python，再试 py -3（和 .cmd 里同一套兜底）。
    private static bool RunLauncher(string root, out string error)
    {
        string[] candidates = { "python", "py" };
        error = "";
        foreach (string exe in candidates)
        {
            string tail = exe == "py" ? " -3" : "";
            var psi = new ProcessStartInfo(exe, tail + " -m stzb_app.tools.launch start --no-browser --quiet")
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = System.Text.Encoding.UTF8,
                StandardErrorEncoding = System.Text.Encoding.UTF8,
            };
            Process p;
            try
            {
                p = Process.Start(psi);
            }
            catch (Exception)
            {
                continue;                       // 这个解释器不存在，换下一个
            }
            string so, se;
            using (p)
            {
                so = p.StandardOutput.ReadToEnd();
                se = p.StandardError.ReadToEnd();
                p.WaitForExit();
                if (p.ExitCode == 0) return true;
                error = "退出码 " + p.ExitCode + "\n" + (se + so).Trim();
                error = error.Length > 1200 ? error.Substring(0, 1200) : error;
            }
            return false;                       // 解释器存在但启动失败，不再试下一个
        }
        error = "找不到 python / py，请安装 Python 3.11+ 并勾选 Add to PATH。";
        return false;
    }

    private static bool Healthy(int port)
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/_stcore/health");
            req.Timeout = 1500;
            req.ReadWriteTimeout = 1500;
            using (var resp = (HttpWebResponse)req.GetResponse())
                return (int)resp.StatusCode == 200;
        }
        catch
        {
            return false;
        }
    }
}
