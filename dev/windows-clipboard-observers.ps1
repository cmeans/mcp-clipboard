<#
.SYNOPSIS
    Disable / restore the Windows 11 clipboard chain observers so the
    mcp-clipboard e2e suite can run in a clean environment.

.DESCRIPTION
    Hypothesis under test: the silent-no-op race observed in mc-005,
    mc-009, mc-017 attempt 1, and mc-020 attempt 1 is caused by external
    processes (Clipboard History service `cbdhsvc`, Cloud Clipboard,
    Suggested Actions text-extractor, third-party clipboard managers)
    racing against our OpenClipboard / EmptyClipboard / SetClipboardData /
    CloseClipboard transaction. WM_CLIPBOARDUPDATE is asynchronously
    posted on Windows 11, so listeners can re-open the clipboard
    immediately after our CloseClipboard.

    This script captures the current state of the four toggles believed
    to matter, applies the test (all observers off), and on a second
    invocation restores the saved state exactly. If a registry value did
    not exist before, Restore deletes the value we created rather than
    leaving a stale 0/1 behind.

    Run as Administrator. The HKLM policy keys require it.

.PARAMETER Mode
    Disable: capture current state to a JSON file, then apply test values.
    Restore: read the saved state file and undo the changes.

.PARAMETER StatePath
    Where to save / read the captured state. Default lives under
    $env:LOCALAPPDATA so it survives reboots and is not shared across
    user profiles.

.EXAMPLE
    .\windows-clipboard-observers.ps1 -Mode Disable
    # reboot the guest, run the e2e suite, then:
    .\windows-clipboard-observers.ps1 -Mode Restore

.NOTES
    Saves to:
      HKLM\SOFTWARE\Policies\Microsoft\Windows\System\AllowClipboardHistory      (DWORD, set to 0)
      HKLM\SOFTWARE\Policies\Microsoft\Windows\System\AllowCrossDeviceClipboard  (DWORD, set to 0)
      HKCU\Software\Microsoft\Windows\CurrentVersion\SmartActionPlatform\SmartClipboard\Disabled (DWORD, set to 1)
      cbdhsvc* service instances (stopped)

    A reboot is recommended after Disable (so the policy keys take
    effect process-wide and the user-mode-service template does not
    respawn under stale settings) and after Restore (to bring
    everything back to a known-good baseline). The script does NOT
    reboot for you.
#>

#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Disable', 'Restore')]
    [string]$Mode,

    [string]$StatePath = (Join-Path $env:LOCALAPPDATA 'mcp-clipboard-observer-state.json')
)

$ErrorActionPreference = 'Stop'

# Registry settings under test. KeyExisted / ValueExisted are captured
# at Disable time so Restore can put the world back exactly as it found
# it (deleting values we created from scratch rather than leaving a 0).
$settings = @(
    [pscustomobject]@{
        Path      = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System'
        Name      = 'AllowClipboardHistory'
        TestValue = 0
        Type      = 'DWord'
        Note      = 'Clipboard History (Win+V)'
    },
    [pscustomobject]@{
        Path      = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System'
        Name      = 'AllowCrossDeviceClipboard'
        TestValue = 0
        Type      = 'DWord'
        Note      = 'Cloud Clipboard cross-device sync'
    },
    [pscustomobject]@{
        Path      = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\SmartActionPlatform\SmartClipboard'
        Name      = 'Disabled'
        TestValue = 1
        Type      = 'DWord'
        Note      = 'Suggested Actions text extractor'
    }
)


function Get-RegistryValueState {
    param([string]$Path, [string]$Name)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{
            KeyExisted   = $false
            ValueExisted = $false
            OriginalValue = $null
        }
    }
    $key = Get-Item -LiteralPath $Path
    if ($key.GetValueNames() -notcontains $Name) {
        return [pscustomobject]@{
            KeyExisted   = $true
            ValueExisted = $false
            OriginalValue = $null
        }
    }
    return [pscustomobject]@{
        KeyExisted   = $true
        ValueExisted = $true
        OriginalValue = (Get-ItemProperty -LiteralPath $Path -Name $Name).$Name
    }
}


function Get-ClipboardServiceInstances {
    # cbdhsvc is a template service; the running instances are
    # cbdhsvc_<sessionhex>. Wildcard catches both so Restore can
    # bring back whatever was running before, in name and state.
    Get-Service -Name 'cbdhsvc*' -ErrorAction SilentlyContinue |
        ForEach-Object {
            [pscustomobject]@{
                Name      = $_.Name
                Status    = [string]$_.Status
                StartType = [string]$_.StartType
            }
        }
}


function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Must run as Administrator. The HKLM policy keys require it."
    }
}


if ($Mode -eq 'Disable') {
    Assert-Admin

    if (Test-Path -LiteralPath $StatePath) {
        throw "State file already exists at $StatePath. Run with -Mode Restore first, or delete the file to start fresh."
    }

    # Phase 1: capture everything BEFORE making any change. If the
    # script is killed between capture and apply, the state file is
    # already on disk and a future Restore still works.
    $state = [pscustomobject]@{
        Timestamp = (Get-Date -Format 'o')
        Registry  = @($settings | ForEach-Object {
            $captured = Get-RegistryValueState -Path $_.Path -Name $_.Name
            [pscustomobject]@{
                Path          = $_.Path
                Name          = $_.Name
                Note          = $_.Note
                Type          = $_.Type
                TestValue     = $_.TestValue
                KeyExisted    = $captured.KeyExisted
                ValueExisted  = $captured.ValueExisted
                OriginalValue = $captured.OriginalValue
            }
        })
        Services  = @(Get-ClipboardServiceInstances)
    }

    $stateDir = Split-Path -Parent $StatePath
    if (-not (Test-Path -LiteralPath $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }
    $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatePath -Encoding UTF8
    Write-Host "Captured current state -> $StatePath" -ForegroundColor Cyan

    Write-Host ""
    Write-Host "Captured registry values:"
    foreach ($r in $state.Registry) {
        $existing = if ($r.ValueExisted) { "= $($r.OriginalValue)" } else { "(not set)" }
        Write-Host "  $($r.Path)\$($r.Name)  $existing  -- $($r.Note)"
    }
    Write-Host ""
    Write-Host "Captured service instances:"
    foreach ($s in $state.Services) {
        Write-Host "  $($s.Name)  status=$($s.Status)  startType=$($s.StartType)"
    }

    # Phase 2: apply test values.
    Write-Host ""
    Write-Host "Applying test values..." -ForegroundColor Yellow
    foreach ($r in $state.Registry) {
        if (-not (Test-Path -LiteralPath $r.Path)) {
            New-Item -Path $r.Path -Force | Out-Null
            Write-Host "  Created key $($r.Path)"
        }
        New-ItemProperty -LiteralPath $r.Path -Name $r.Name -Value $r.TestValue -PropertyType $r.Type -Force | Out-Null
        Write-Host "  Set $($r.Path)\$($r.Name) = $($r.TestValue)"
    }

    foreach ($s in $state.Services) {
        if ($s.Status -eq 'Running') {
            try {
                Stop-Service -Name $s.Name -Force -ErrorAction Stop
                Write-Host "  Stopped service $($s.Name)"
            }
            catch {
                Write-Warning "  Failed to stop $($s.Name): $($_.Exception.Message)"
            }
        }
    }

    Write-Host ""
    Write-Host "Reboot the guest before running the test suite." -ForegroundColor Green
    Write-Host "After testing, run this script with -Mode Restore to put the original settings back."
}
elseif ($Mode -eq 'Restore') {
    Assert-Admin

    if (-not (Test-Path -LiteralPath $StatePath)) {
        throw "No state file at $StatePath. Nothing to restore (or the file was deleted)."
    }

    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    Write-Host "Restoring state captured at $($state.Timestamp)" -ForegroundColor Cyan

    foreach ($r in $state.Registry) {
        if ($r.ValueExisted) {
            # Value existed before; restore the original.
            if (-not (Test-Path -LiteralPath $r.Path)) {
                New-Item -Path $r.Path -Force | Out-Null
            }
            New-ItemProperty -LiteralPath $r.Path -Name $r.Name -Value $r.OriginalValue -PropertyType $r.Type -Force | Out-Null
            Write-Host "  Restored $($r.Path)\$($r.Name) = $($r.OriginalValue)"
        }
        else {
            # Value did NOT exist before; remove the one we created.
            if ((Test-Path -LiteralPath $r.Path) -and ((Get-Item -LiteralPath $r.Path).GetValueNames() -contains $r.Name)) {
                Remove-ItemProperty -LiteralPath $r.Path -Name $r.Name -Force
                Write-Host "  Removed $($r.Path)\$($r.Name) (did not exist before)"
            }
            # Leave the key itself alone; deleting policy keys we did
            # not create is out of scope for a Restore action.
        }
    }

    foreach ($s in $state.Services) {
        if ($s.Status -eq 'Running') {
            try {
                Start-Service -Name $s.Name -ErrorAction Stop
                Write-Host "  Started service $($s.Name)"
            }
            catch {
                Write-Warning "  Failed to start $($s.Name): $($_.Exception.Message)  (a reboot will respawn it from its template)"
            }
        }
    }

    Remove-Item -LiteralPath $StatePath -Force
    Write-Host ""
    Write-Host "Restored original state. State file removed." -ForegroundColor Green
    Write-Host "Reboot recommended to confirm clipboard chain observers are back to their pre-test configuration."
}
