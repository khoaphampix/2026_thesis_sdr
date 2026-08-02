pyenv activate  pysdr_3_11_9

# MacBook — PlutoSDR Video Receiver

## 1. Role of this computer

Use the MacBook as the receiver:

```text
RF channel
→ PlutoSDR RX
→ repeated-preamble synchronization
→ carrier-frequency-offset correction
→ BPSK demodulation
→ CRC validation
→ duplicate removal and sequence reordering
→ delayed HEVC buffer
→ FFplay
```

Run:

```text
pluto_video_rx.py
```

Start this program before the Windows/WSL transmitter.

## 2. Pluto connection used in this project

Use direct USB only:

```bash
--uri "usb:"
```

Do not configure a Pluto IP address, `pluto.local`, or another network context.

This works because only one Pluto is directly attached to the MacBook.

The Mac receiver does not need access to the RTSP camera.

## 3. Updated automatic logging

The receiver now creates:

```text
logs/rx_YYYYMMDD_HHMMSS.log
```

The log contains:

- start and finish timestamps;
- complete command line;
- RX settings;
- detected TX session;
- every new valid packet when `--status-every 1`;
- duplicates, pending packets and skipped packets;
- synchronization metric;
- current and average CFO;
- useful RX bitrate;
- FFplay messages and Python errors;
- final RX summary.

Use a custom filename:

```bash
--log-file logs/rx_test_01.log
```

## 4. Install macOS prerequisites

Install Apple command-line tools:

```bash
xcode-select --install
```

Install Homebrew when it is not already available.

Then install the required tools:

```bash
brew update
brew install python ffmpeg cmake pkg-config libusb
brew install tfcollins/libiio
```

Verify:

```bash
python3 --version
ffmpeg -version
ffplay -version
iio_info --version
```

## 5. Create the Mac project environment

```bash
mkdir -p ~/two_pluto_video
cd ~/two_pluto_video
```

Copy `pluto_video_rx.py` into this folder.

Create the environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install numpy pyadi-iio
mkdir -p logs
```

Verify:

```bash
python3 - <<'PY'
import numpy
import adi

print("NumPy:", numpy.__version__)
print("pyadi-iio import: OK")
PY
```

Activate this environment in every new terminal:

```bash
cd ~/two_pluto_video
source .venv/bin/activate
```

## 6. Connect the RX Pluto directly by USB

Connect the Pluto using a data-capable USB cable.

Check:

```bash
iio_info -s
```

The run command uses:

```bash
--uri "usb:"
```

Quick test:

```bash
python3 - <<'PY'
import adi

sdr = adi.Pluto(uri="usb:")
print("RX Pluto USB connection OK")
print("Sample rate:", sdr.sample_rate)
PY
```

No Pluto Ethernet or USB-network setup is required for this project.

## 7. Start the Mac receiver

```bash
cd ~/two_pluto_video
source .venv/bin/activate
```

Run:

```bash
python3 pluto_video_rx.py \
--uri "usb:" \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--rx-gain 20 \
--rx-buffer-frames 4 \
--candidates-per-phase 8 \
--metric-threshold 0.55 \
--reorder-window 8 \
--gap-timeout 1.0 \
--playback-prebuffer-bytes 8000 \
--status-every 1 \
--log-dir logs \
--rx-save two_pluto_received.h265
```

Leave it running, then start TX on Windows/WSL.

## 8. Healthy RX output

```text
New TX session: 0x12345678; starting at sequence 0
RX seq=20,
valid=21,
duplicates=34,
pending=0,
metric=0.87,
avg_metric=0.85,
CFO=+12500 Hz,
avg_CFO=+12420 Hz,
rate=27,000 bit/s
```

Interpretation:

| Field | Meaning |
|---|---|
| `valid` | New CRC-valid packets |
| `duplicates` | Repeated packet copies; normal |
| `pending` | Out-of-order packets waiting |
| `metric` | Synchronization quality |
| `CFO` | Frequency offset between independent Plutos |
| `rate` | Useful reconstructed HEVC bitrate |

Good first-test conditions:

```text
valid continuously increases
metric normally above about 0.70
CFO is reasonably stable
pending stays small
skipped packets stay zero or low
```

## 9. Playback delay

The initial delayed playback is controlled by:

```bash
--playback-prebuffer-bytes 8000
```

Larger value:

```text
more startup delay
better protection from short RF interruptions
```

Smaller value:

```text
less startup delay
greater risk of playback starvation
```

Suggested tests:

```text
5000 bytes
8000 bytes
12000 bytes
```

## 10. Stop and inspect results

Use:

```text
Ctrl+C
```

Produced files:

```text
two_pluto_received.h265
logs/rx_YYYYMMDD_HHMMSS.log
```

Play the saved stream again:

```bash
ffplay -f hevc two_pluto_received.h265
```

Inspect:

```bash
ffprobe \
-v error \
-f hevc \
-show_streams \
two_pluto_received.h265
```

## 11. No valid packets

Confirm the Windows and Mac commands match exactly:

```text
frequency       915000000
sample rate     2000000
samples/bit     4
payload size    400
```

Try:

```bash
--rx-gain 30
```

Lower the synchronization threshold cautiously:

```bash
--metric-threshold 0.45
```

Ask the TX side to increase packet airtime:

```bash
--packet-airtime 0.15
```

Or increase TX gain gradually.

## 12. Many skipped packets

Increase tolerance:

```bash
--rx-buffer-frames 6 \
--reorder-window 12 \
--gap-timeout 1.5 \
--playback-prebuffer-bytes 12000
```

Also ask TX to return to a longer packet airtime.

## 13. Many invalid RX buffers

Some invalid buffers are normal because RX captures may begin between packets.

A problem exists when:

```text
valid does not increase
invalid buffers increase continuously
```

Check RF gain, frequency match, antennas, cable attenuation and TX activity.

## 14. FFplay does not start

FFplay starts only after the received HEVC data reaches the configured prebuffer.

For a quick test:

```bash
--playback-prebuffer-bytes 2000
```

For receiver testing without display:

```bash
--no-display
```

The `.h265` file and RX log will still be saved.

## 15. Mac can also become TX later

The Mac can run `pluto_video_tx.py` after:

- direct USB Pluto access works with `uri="usb:"`;
- FFmpeg has `libx265`;
- the Mac can reach the RTSP camera;
- the camera URL is configured in the TX script.

For the first successful experiment, keep:

```text
Windows/WSL = TX
MacBook     = RX
```

Then reverse the roles as a later portability test.
