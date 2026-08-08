# reset_attach_usb.ps1

$PlutoHardwareId = "0456:b673"

Write-Host "Finding PlutoSDR..."

$PlutoLine = usbipd list |
    Select-String -SimpleMatch $PlutoHardwareId |
    Select-Object -First 1

if (-not $PlutoLine) {
    Write-Error "PlutoSDR $PlutoHardwareId was not found."
    usbipd list
    exit 1
}

$BusId = ($PlutoLine.Line.Trim() -split "\s+")[0]

Write-Host "PlutoSDR BUSID: $BusId"

Write-Host "Detaching PlutoSDR from WSL..."
usbipd detach --busid $BusId 2>$null

Start-Sleep -Seconds 2

Write-Host "Attaching PlutoSDR to WSL..."
usbipd attach --wsl --busid $BusId

if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to attach PlutoSDR."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Current USBIP status:"
usbipd list

Write-Host ""
Write-Host "Checking PlutoSDR inside Ubuntu..."
wsl -d Ubuntu-22.04 -- bash -lc "lsusb | grep -i '0456:b673'"

Write-Host ""
Write-Host "PlutoSDR reset and attachment completed."


Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

Set-Location "\\wsl.localhost\Ubuntu-22.04\home\kev\pycode\2026_thesis_sdr\sh"
 .\reset_attach_usb.ps1