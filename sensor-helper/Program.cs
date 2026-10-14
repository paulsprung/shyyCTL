// Minimal background HTTP server exposing CPU/motherboard/RAM sensor
// data straight from LibreHardwareMonitorLib - built to replace running
// the full LibreHardwareMonitor GUI app just to get its Remote Web
// Server. Serves the same {Text, Value, Children} nested JSON tree
// shape app/sensors.py's lhm_temp() already knows how to walk, at the
// same config.yaml URL - only what's running behind that URL changed,
// no Python-side parsing changes needed.
//
// Needs to run elevated (same reasoning as OpenRGB's RAM SPD access) -
// MSR/SuperIO reads require administrator rights on Windows; launched
// that way from main.py's _launch_vendor_sensor_helper(), same
// ShellExecuteW("runas") pattern already used for OpenRGB.
//
// Logs to SensorHelper.log next to the exe, not the console - this is
// built as WinExe (no console window at all, see SensorHelper.csproj's
// own comment on why), and it's launched via ShellExecuteW/Popen
// without redirecting stdout/stderr, so anything written to
// Console.WriteLine/Console.Error here was silently going nowhere.
// Without this, a real startup failure (e.g. computer.Open() throwing
// before the HTTP listener ever binds) would leave nothing running on
// the configured port and absolutely no trace of why - exactly the
// "still shows -- degC, no idea why" situation this exists to prevent.

using System.Globalization;
using System.Net;
using System.Text;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

string logPath = Path.Combine(AppContext.BaseDirectory, "SensorHelper.log");

void Log(string message)
{
    try
    {
        File.AppendAllText(logPath, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {message}{Environment.NewLine}");
    }
    catch
    {
        // If even logging fails (read-only directory, etc.) there's
        // nowhere left to report that - swallow rather than crash the
        // whole process over a failed log write.
    }
}

try
{
    int port = 8085;
    if (args.Length > 0 && int.TryParse(args[0], out var parsedPort))
    {
        port = parsedPort;
    }

    Log($"Starting, target port {port}");

    var computer = new Computer
    {
        IsCpuEnabled = true,
        IsMotherboardEnabled = true,
        IsMemoryEnabled = true,
    };
    try
    {
        computer.Open();
        Log($"computer.Open() succeeded - {computer.Hardware.Count} top-level hardware node(s): "
            + string.Join(", ", computer.Hardware.Select(h => $"{h.Name} ({h.HardwareType})")));
    }
    catch (Exception ex)
    {
        // Confirmed real risk, not theoretical: this call needs
        // administrator rights (MSR/SuperIO access) and can throw if
        // elevation didn't actually take effect, or the underlying
        // driver couldn't load for any reason - previously unguarded,
        // so a failure here killed the whole process before the HTTP
        // listener ever started, with zero trace anywhere.
        Log($"computer.Open() FAILED: {ex}");
        return 1;
    }

    var listener = new HttpListener();
    listener.Prefixes.Add($"http://127.0.0.1:{port}/");
    try
    {
        listener.Start();
        Log($"Listening on http://127.0.0.1:{port}/data.json");
    }
    catch (HttpListenerException ex)
    {
        Log($"Could not bind http://127.0.0.1:{port}/ - {ex.Message}");
        return 1;
    }

    while (true)
    {
        HttpListenerContext context;
        try
        {
            context = listener.GetContext();
        }
        catch (HttpListenerException)
        {
            break; // listener stopped/disposed
        }

        try
        {
            if (context.Request.Url?.AbsolutePath == "/data.json")
            {
                foreach (var hardware in computer.Hardware)
                {
                    UpdateRecursive(hardware);
                }
                var tree = BuildNode("Computer", null, computer.Hardware.Select(BuildHardwareNode).ToList());
                var json = JsonSerializer.Serialize(tree);
                var bytes = Encoding.UTF8.GetBytes(json);
                context.Response.ContentType = "application/json";
                context.Response.ContentLength64 = bytes.Length;
                context.Response.OutputStream.Write(bytes, 0, bytes.Length);
            }
            else
            {
                context.Response.StatusCode = 404;
            }
        }
        catch (Exception ex)
        {
            Log($"Request handling failed: {ex}");
            try { context.Response.StatusCode = 500; } catch { /* response already broken */ }
        }
        finally
        {
            context.Response.OutputStream.Close();
        }
    }

    return 0;
}
catch (Exception ex)
{
    // Belt and braces: anything not already caught above (e.g. the
    // HttpListenerContext.Request.Url access itself, or something in
    // argument parsing) still gets logged instead of vanishing.
    Log($"Unhandled exception, exiting: {ex}");
    return 1;
}

// -- helpers --------------------------------------------------------------

static void UpdateRecursive(IHardware hardware)
{
    hardware.Update();
    foreach (var sub in hardware.SubHardware)
    {
        UpdateRecursive(sub);
    }
}

static Dictionary<string, object?> BuildNode(string text, string? value, List<Dictionary<string, object?>> children)
{
    return new Dictionary<string, object?>
    {
        ["Text"] = text,
        ["Value"] = value,
        ["Children"] = children,
    };
}

static Dictionary<string, object?> BuildHardwareNode(IHardware hardware)
{
    var children = new List<Dictionary<string, object?>>();
    foreach (var sensor in hardware.Sensors)
    {
        children.Add(BuildSensorNode(sensor));
    }
    foreach (var sub in hardware.SubHardware)
    {
        children.Add(BuildHardwareNode(sub));
    }
    return BuildNode(hardware.Name, null, children);
}

static Dictionary<string, object?> BuildSensorNode(ISensor sensor)
{
    string? value = sensor.Value is float v ? FormatValue(v, sensor.SensorType) : null;
    return BuildNode(sensor.Name, value, new List<Dictionary<string, object?>>());
}

static string FormatValue(float value, SensorType type)
{
    // Only Temperature actually matters to app/sensors.py's lhm_temp()
    // (it only ever looks for a "°C" substring) - the rest are formatted
    // anyway so this JSON is genuinely useful if inspected directly,
    // same spirit as the real LHM data.json. Invariant culture: plain
    // string interpolation follows the Windows display locale, which on a
    // German system writes "45,3°C" - not a number Python's float() reads.
    var inv = CultureInfo.InvariantCulture;
    return type switch
    {
        SensorType.Temperature => value.ToString("0.0", inv) + "°C",
        SensorType.Load => value.ToString("0.0", inv) + "%",
        SensorType.Clock => value.ToString("0", inv) + "MHz",
        SensorType.Voltage => value.ToString("0.000", inv) + "V",
        SensorType.Fan => value.ToString("0", inv) + "RPM",
        SensorType.Power => value.ToString("0.0", inv) + "W",
        _ => value.ToString("0.0", inv),
    };
}
