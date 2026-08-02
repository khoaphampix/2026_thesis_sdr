# Windows PC with WSL — PlutoSDR Video Transmitter

## 1. Role of this computer

Use the Windows PC with WSL Ubuntu as the transmitter:

```text
RTSP camera
→ FFmpeg low-bitrate HEVC encoder
→ packet header + sequence + CRC
→ BPSK modulation
→ PlutoSDR TX
→ RF channel
```

Run:

```text
pluto_video_tx.py
```

The MacBook should run `pluto_video_rx.py` first.

## 2. Pluto connection used in this project

Use direct USB only:

```bash
--uri "usb:"
```

Do not use a Pluto IP address, `pluto.local`, or another network context.

This works because only one Pluto is attached to WSL.

The camera remains a LAN device at:

```text
192.168.1.2
```

That address belongs to the camera, not the PlutoSDR.

## 3. Updated automatic logging

The transmitter now writes a persistent log automatically:

```text
logs/tx_YYYYMMDD_HHMMSS.log
```

The same information remains visible in the terminal.

The log contains:

- start and finish timestamps;
- complete command line;
- radio and video settings;
- FFmpeg warnings and errors;
- every transmitted packet when `--status-every 1`;
- payload rate and source-buffer delay;
- final TX packet count, bytes and useful bitrate;
- fatal Python errors.

Use a custom filename when required:

```bash
--log-file logs/tx_test_01.log
```

Or select another directory:

```bash
--log-dir experiment_logs
```

## 4. Copy the TX file into WSL

```bash
mkdir -p ~/pycode/two_pluto_video
cd ~/pycode/two_pluto_video
```

Copy `pluto_video_tx.py` into this folder.

Create the log directory:

```bash
mkdir -p logs
```

## 5. Activate the existing Python environment

When the existing project environment already works:

```bash
cd ~/pycode/two_pluto_video
source .venv/bin/activate
```

If a new environment is needed:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install numpy pyadi-iio
```

Verify:

```bash
python3 -c "import numpy, adi; print('Python SDR packages OK')"
ffmpeg -version
iio_info -s
```

## 6. Attach the TX Pluto to WSL

Open Windows PowerShell as Administrator:

```powershell
usbipd list
```

Find the Pluto bus ID and attach it:

```powershell
usbipd bind --busid <BUSID>
usbipd attach --wsl --busid <BUSID>
```

Inside WSL, verify USB discovery:

```bash
iio_info -s
```

The Python command still uses the generic direct USB context:

```bash
--uri "usb:"
```

Quick connection test:

```bash
python3 - <<'PY'
import adi

sdr = adi.Pluto(uri="usb:")
print("TX Pluto USB connection OK")
print("Sample rate:", sdr.sample_rate)
PY
```

## 7. Configure the RTSP camera

Open the transmitter file:

```bash
nano pluto_video_tx.py
```

Find:

```python
CAMERA_URL = (
    "rtsp://admin:PASSWORD@192.168.1.2:554/Preview_01_sub"
)
```

Replace `PASSWORD` locally.

Do not publish the real password in a thesis appendix or public repository.

## 8. Test the camera before SDR transmission

```bash
ffmpeg \
-rtsp_transport tcp \
-i "rtsp://admin:PASSWORD@192.168.1.2:554/Preview_01_sub" \
-t 4 \
-map 0:v:0 \
-c:v libx265 \
-f hevc \
-y camera_check.h265
```

Play:

```bash
ffplay -f hevc camera_check.h265
```

Continue only when this video works.

## 9. First known-file TX test

Start the Mac receiver first.

Then run on WSL:

```bash
python3 pluto_video_tx.py \
--uri "usb:" \
--source file \
--input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--packet-airtime 0.10 \
--inter-packet-gap 0.002 \
--tx-gain -50 \
--status-every 1 \
--log-dir logs \
--tx-save two_pluto_file_tx.h265
```

Expected log example:

```text
logs/tx_20260802_151500.log
```

## 10. Live RTSP TX command

Start the Mac receiver first, then run:

```bash
python3 pluto_video_tx.py \
--uri "usb:" \
--source rtsp \
--duration 0 \
--video-size 320x180 \
--fps 5 \
--video-bitrate 5 \
--keyint-seconds 10 \
--encoder-preset veryfast \
--camera-buffer-bytes 524288 \
--camera-read-size 1024 \
--source-timeout 15 \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--packet-airtime 0.10 \
--inter-packet-gap 0.002 \
--tx-gain -50 \
--status-every 1 \
--log-dir logs \
--tx-save two_pluto_live_tx.h265
```

Use `Ctrl+C` to stop.

## 11. Healthy TX output

```text
TX packet=99,
bytes=40,000,
rate=28,000 bit/s,
source_buffer=8,000 B (~12.8s)
```

The source buffer can fluctuate. It should not increase continuously for the entire test.

Continuous growth means:

```text
encoded camera rate > SDR delivery rate
```

Reduce one of:

```bash
--video-bitrate 4
--fps 4
--video-size 256x144
```

## 12. Faster TX after the baseline works

Change only packet airtime:

```text
0.10 → conservative
0.09
0.08
0.07
0.06 → faster but less repetition
```

Example:

```bash
--packet-airtime 0.08
```

Return to the previous value if the Mac reports many skipped packets.

## 13. Higher-resolution profiles

Recommended baseline:

```bash
--video-size 320x180 \
--fps 5 \
--video-bitrate 5
```

Next test:

```bash
--video-size 426x240 \
--fps 4 \
--video-bitrate 6
```

Experimental:

```bash
--video-size 640x360 \
--fps 3 \
--video-bitrate 8
```

The actual video production rate must stay below useful RF delivery throughput.

## 14. TX files produced

Typical run files:

```text
two_pluto_live_tx.h265
logs/tx_YYYYMMDD_HHMMSS.log
```

The `.h265` file is the exact encoded stream supplied to the TX packetizer.

## 15. TX troubleshooting

### Pluto not found

```bash
iio_info -s
```

Then reattach through `usbipd` and retry:

```bash
python3 -c "import adi; print(adi.Pluto(uri='usb:').sample_rate)"
```

### Camera fails

Run the standalone FFmpeg test again. Confirm WSL can reach:

```text
192.168.1.2
```

### Mac receives nothing

Check that both programs use exactly:

```text
frequency       915000000
sample rate     2000000
samples/bit     4
payload size    400
```

Increase TX power gradually:

```text
-50, -45, -40, -35, -30 dB
```

Do not start at maximum TX power when the radios are close.

## 16. RF safety

Do not connect Pluto TX directly to Pluto RX through an unattenuated coaxial cable.

Use suitable attenuation for a cable test. With antennas, start at low TX power and short range.
