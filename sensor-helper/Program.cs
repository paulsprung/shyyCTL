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

using System.Net;
using System.Text;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

int port = 8085;
if (args.Length > 0 && int.TryParse(args[0], out var parsedPort))
{
    port = parsedPort;
}

var computer = new Computer
{
    IsCpuEnabled = true,
    IsMotherboardEnabled = true,
    IsMemoryEnabled = true,
};
computer.Open();

var listener = new HttpListener();
listener.Prefixes.Add($"http://127.0.0.1:{port}/");
try
{
    listener.Start();
}
catch (HttpListenerException ex)
{
    Console.Error.WriteLine($"Could not bind http://127.0.0.1:{port}/ - {ex.Message}");
    return 1;
}

Console.WriteLine($"SensorHelper listening on http://127.0.0.1:{port}/data.json");

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
        Console.Error.WriteLine($"Request handling failed: {ex.Message}");
        try { context.Response.StatusCode = 500; } catch { /* response already broken */ }
    }
    finally
    {
        context.Response.OutputStream.Close();
    }
}

return 0;

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
    // same spirit as the real LHM data.json.
    return type switch
    {
        SensorType.Temperature => $"{value:0.0}°C",
        SensorType.Load => $"{value:0.0}%",
        SensorType.Clock => $"{value:0}MHz",
        SensorType.Voltage => $"{value:0.000}V",
        SensorType.Fan => $"{value:0}RPM",
        SensorType.Power => $"{value:0.0}W",
        _ => $"{value:0.0}",
    };
}
