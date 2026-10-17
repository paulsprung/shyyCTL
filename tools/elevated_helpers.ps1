<#
Sets up (or removes) starting OpenRGB and SensorHelper with admin rights
without a UAC prompt at every login - run once, elevated, from shyyctl's
Settings page (main.py's /api/system/elevated-helpers), or by the
uninstaller with -Action remove.

Both helpers need admin rights (RAM RGB over SMBus, CPU temperatures -
both through the PawnIO driver). Starting them silently with those rights
is only safe if nothing a normal user process can change ends up running
elevated, so:
  - the helpers are copied to %ProgramFiles%\shyyctl\helpers, which only
    administrators can write to (their install folder under
    %LOCALAPPDATA% is writable by any program the user runs);
  - OpenRGB gets its configuration folder - which is also where it loads
    plugins from - in there too (--config), instead of %APPDATA%\OpenRGB;
  - SensorHelper is built to keep its native DLLs next to the exe rather
    than unpacking them into %TEMP% (sensor-helper/SensorHelper.csproj);
  - two scheduled tasks ("\shyyctl\OpenRGB", "\shyyctl\SensorHelper")
    start exactly those copies, with fixed arguments, at the highest run
    level for this user. shyyctl runs them with schtasks /run.

Writes {"ok": bool, "message": str} to -ResultFile, since an elevated
process' exit code doesn't reach the non-elevated caller.
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('install', 'remove')][string]$Action,
    [string]$VendorDir,
    [string]$User,
    [string]$OpenRgbSettings,
    [int]$SensorPort = 8085,
    [string]$ResultFile
)

$ErrorActionPreference = 'Stop'
$Dest = Join-Path $env:ProgramFiles 'shyyctl\helpers'
$TaskPath = '\shyyctl\'
$Helpers = @('OpenRGB', 'SensorHelper')

function Write-Result([bool]$ok, [string]$message) {
    if ($ResultFile) {
        @{ ok = $ok; message = $message } | ConvertTo-Json | Out-File -Encoding utf8 -FilePath $ResultFile
    }
}

function Stop-Helpers {
    # every running copy, whichever folder it was started from - the
    # files can't be replaced or removed while one is running
    Get-Process -Name $Helpers -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
}

try {
    if ($Action -eq 'remove') {
        foreach ($name in $Helpers) {
            Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        }
        if (Test-Path $Dest) {
            Get-Process -Name $Helpers -ErrorAction SilentlyContinue |
                Where-Object { $_.Path -and $_.Path.StartsWith($Dest, [StringComparison]::OrdinalIgnoreCase) } |
                Stop-Process -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 800
            Remove-Item -LiteralPath $Dest -Recurse -Force
        }
        $parent = Split-Path $Dest
        if ((Test-Path $parent) -and -not (Get-ChildItem -LiteralPath $parent -Force)) {
            Remove-Item -LiteralPath $parent -Force
        }
        Write-Result $true 'removed'
        exit 0
    }

    if (-not $VendorDir -or -not (Test-Path $VendorDir)) { throw "vendor folder not found: $VendorDir" }
    if (-not $User) { throw 'no user given' }

    Stop-Helpers
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    foreach ($name in $Helpers) {
        $src = Join-Path $VendorDir $name
        $dst = Join-Path $Dest $name
        if (Test-Path $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
        if (Test-Path $src) { Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force }
    }

    # OpenRGB's settings - and the plugins it would load from next to them -
    # in a folder only administrators can write to. Starts from the user's
    # current OpenRGB.json (detectors, SDK server) and zone sizes; plugins
    # are deliberately not carried over.
    $config = Join-Path $Dest 'OpenRGB-config'
    if (Test-Path $config) { Remove-Item -LiteralPath $config -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $config | Out-Null
    if ($OpenRgbSettings -and (Test-Path $OpenRgbSettings)) {
        Copy-Item -LiteralPath $OpenRgbSettings -Destination (Join-Path $config 'OpenRGB.json')
        $sizes = Join-Path (Split-Path $OpenRgbSettings) 'sizes.ors'
        if (Test-Path $sizes) { Copy-Item -LiteralPath $sizes -Destination (Join-Path $config 'sizes.ors') }
    }

    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest

    $openrgb = Get-ChildItem -LiteralPath (Join-Path $Dest 'OpenRGB') -Recurse -Filter 'OpenRGB.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($openrgb) {
        $action = New-ScheduledTaskAction -Execute $openrgb.FullName `
            -Argument "--server --startminimized --config `"$config`"" -WorkingDirectory $openrgb.DirectoryName
        Register-ScheduledTask -TaskPath $TaskPath -TaskName 'OpenRGB' -Action $action -Principal $principal `
            -Settings $settings -Description 'Started by shyyctl: OpenRGB with admin rights for RAM RGB, without a UAC prompt.' `
            -Force | Out-Null
    }
    $sensor = Join-Path $Dest 'SensorHelper\SensorHelper.exe'
    if (Test-Path $sensor) {
        $action = New-ScheduledTaskAction -Execute $sensor -Argument "$SensorPort" -WorkingDirectory (Split-Path $sensor)
        Register-ScheduledTask -TaskPath $TaskPath -TaskName 'SensorHelper' -Action $action -Principal $principal `
            -Settings $settings -Description 'Started by shyyctl: SensorHelper with admin rights for temperatures, without a UAC prompt.' `
            -Force | Out-Null
    }

    @{ sensor_port = $SensorPort; user = $User; installed = (Get-Date).ToString('o') } |
        ConvertTo-Json | Out-File -Encoding utf8 -FilePath (Join-Path $Dest 'setup.json')

    # Start the protected copies right away, so this takes effect now.
    foreach ($name in $Helpers) {
        if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskPath $TaskPath -TaskName $name
        }
    }
    Write-Result $true 'installed'
    exit 0
}
catch {
    Write-Result $false $_.Exception.Message
    exit 1
}
