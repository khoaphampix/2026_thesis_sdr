brew install \
    ffmpeg \
    cmake \
    git \
    pkg-config \
    libxml2 \
    libzstd \
    libusb \
    libserialport \
    zstd


      3.11.9/envs/pysdr_3_11_9



# Two-PlutoSDR HEVC Video Streaming Setup

## 1. Project objective

This package separates the radio system into two independent programs:

```text
Windows PC / WSL                         MacBook
----------------                         -------
RTSP camera                              PlutoSDR #2
FFmpeg HEVC encoder                      continuous RX
packet framing + CRC                     synchronization + CFO correction
PlutoSDR #1 continuous TX   ~~~ RF ~~~> duplicate removal + reorder buffer
                                            delayed FFplay video
```

Files:

- `pluto_video_tx.py` — run on the computer connected to the transmitting PlutoSDR.
- `pluto_video_rx.py` — run on the computer connected to the receiving PlutoSDR.

The two programs use a one-way simplex stream. There is no ACK return channel. The transmitter repeats each packet for a short period, and the receiver removes duplicate copies.

---

## 2. Recommended PC roles

### Recommended first configuration

| Computer | Role | Program |
|---|---|---|
| Windows PC with WSL Ubuntu | **Transmitter** | `pluto_video_tx.py` |
| MacBook | **Receiver** | `pluto_video_rx.py` |

### Why this arrangement is recommended

Use the Windows/WSL computer as TX first because:

1. The RTSP camera and FFmpeg camera pipeline have already been tested successfully there.
2. The camera is on the same local LAN as the Windows PC.
3. The TX side performs the real-time HEVC encoding and is the more complicated side.
4. The existing Pluto USB/WSL connection is already working.
5. The MacBook receiver only needs Pluto RX, packet decoding, a video buffer, and FFplay.

Use the MacBook as RX first because it does not need access to the RTSP camera. It only needs one PlutoSDR and the receiver software.

### Can the roles be reversed?

Yes. Either computer can run TX or RX after its Pluto, libiio, Python packages, NumPy, and FFmpeg are working.

A later reversed arrangement is possible:

```text
MacBook = TX
Windows/WSL = RX
```

For MacBook TX, the Mac must also:

- reach the RTSP camera at `192.168.1.2`;
- run FFmpeg with `libx265`;
- sustain real-time encoding;
- have the camera URL configured in `pluto_video_tx.py`.

The recommended initial arrangement remains:

```text
Windows/WSL = TX
MacBook = RX
```

---

## 3. Hardware required

- Two ADALM-PlutoSDR devices.
- Two computers:
  - Windows PC running WSL Ubuntu.
  - MacBook running macOS.
- One suitable USB data cable for each Pluto.
- Two antennas suitable for the selected test frequency, or a coaxial test path with RF attenuation.
- The RTSP camera connected to the same LAN as the TX computer.
- The substream URL, for example:

```text
rtsp://admin:PASSWORD@192.168.1.2:554/Preview_01_sub
```

Replace `PASSWORD` locally. Do not commit a real password to Git or publish it in thesis files.

### RF safety

Do not directly connect Pluto TX to Pluto RX with an unattenuated coaxial cable. Use suitable RF attenuation, commonly tens of decibels, to prevent receiver overload or damage.

When using antennas:

- begin with low TX gain;
- use a permitted local test frequency;
- keep the devices close for the first test;
- increase power gradually only when necessary.

---

## 4. Shared radio settings

The following settings must match on TX and RX:

| Setting | Initial value |
|---|---:|
| Frequency | 915,000,000 Hz |
| Sample rate | 2,000,000 samples/s |
| Samples per bit | 4 |
| Payload size | 400 bytes |

If one value differs, the receiver will not decode the stream.

The first conservative TX dwell is:

```text
packet airtime = 0.10 seconds
```

Approximate dwell-limited payload rate:

```text
400 bytes × 8 / 0.10 seconds = 32,000 bit/s
```

After the two-Pluto link is stable, test shorter airtimes gradually:

```text
0.10 s → approximately 32 kbit/s
0.08 s → approximately 40 kbit/s
0.06 s → approximately 53 kbit/s
```

A shorter dwell is faster but gives the receiver fewer repeated copies of each packet.

---

# Part A — Windows/WSL transmitter preparation

## 5. Copy the transmitter file

Inside WSL:

```bash
mkdir -p ~/pycode/two_pluto_video
cd ~/pycode/two_pluto_video
```

Copy `pluto_video_tx.py` into that directory.

Then:

```bash
chmod u+rw pluto_video_tx.py
```

Running it with `python3` does not require executable permission.

---

## 6. Install or verify WSL dependencies

The existing WSL Pluto environment may already contain these packages.

Check:

```bash
python3 --version
ffmpeg -version
python3 -c "import numpy; print(numpy.__version__)"
python3 -c "import adi; print('pyadi-iio OK')"
iio_info -s
```

If packages are missing:

```bash
sudo apt update
sudo apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    ffmpeg \
    libiio0 \
    libiio-dev \
    libiio-utils \
    libusb-1.0-0-dev
```

Create a virtual environment:

```bash
cd ~/pycode/two_pluto_video
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install numpy pylibiio pyadi-iio
```

Every new WSL terminal should activate it:

```bash
cd ~/pycode/two_pluto_video
source .venv/bin/activate
```

---

## 7. Attach TX Pluto to WSL

From Windows PowerShell as Administrator:

```powershell
usbipd list
```

Identify the Pluto USB bus ID, then:

```powershell
usbipd bind --busid <BUSID>
usbipd attach --wsl --busid <BUSID>
```

Back in WSL:

```bash
iio_info -s
```

Example:

```text
usb:1.4.5
```

Use the exact URI shown by your computer.

A quick Python test:

```bash
python3 - <<'PY'
import adi

uri = "usb:1.4.5"  # replace with the TX Pluto URI
sdr = adi.Pluto(uri=uri)
print("TX Pluto connected")
print("Sample rate:", sdr.sample_rate)
PY
```

If only one Pluto is visible inside WSL, `--uri` can be omitted. An explicit URI is safer.

---

## 8. Configure the RTSP camera in the TX file

Open:

```bash
nano pluto_video_tx.py
```

Near the top:

```python
CAMERA_URL = (
    "rtsp://admin:PASSWORD@192.168.1.2:554/Preview_01_sub"
)
```

Replace only `PASSWORD`.

Save:

```text
Ctrl+O
Enter
Ctrl+X
```

Test camera access from WSL before involving SDR:

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

Play it:

```bash
ffplay -f hevc camera_check.h265
```

---

# Part B — MacBook receiver preparation

## 9. Confirm Mac architecture

On the Mac:

```bash
uname -m
```

Typical output:

```text
arm64   # Apple Silicon
x86_64  # Intel Mac
```

The setup works on either architecture, but all Homebrew and Python packages should use the same architecture. Avoid mixing an Intel/Rosetta Python with Apple Silicon Homebrew libraries.

---

## 10. Install Apple command-line tools and Homebrew

Install command-line tools:

```bash
xcode-select --install
```

Install Homebrew if it is not already installed:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

After installation, follow the Homebrew terminal instructions to add `brew` to the shell PATH.

Check:

```bash
brew --version
```

---

## 11. Install Mac prerequisites

Install Python, FFmpeg and build tools:

```bash
brew update
brew install \
    python@3.13 \
    ffmpeg \
    cmake \
    git \
    pkg-config \
    libxml2 \
    libzstd \
    libusb \
    libserialport \
    zstd
```

Verify video tools:

```bash
ffmpeg -version
ffplay -version
python3 --version
```

Homebrew FFmpeg provides the encoder/decoder tools required by the scripts.

---

## 12. Install libiio on macOS

### Recommended first method

ADI's current macOS build documentation lists this Homebrew formula:

```bash
brew install tfcollins/libiio
```

Then check:

```bash
iio_info --version
iio_info -s
```

### Manual fallback method

Use this only if the formula fails or Python cannot find libiio.

```bash
mkdir -p ~/dev
cd ~/dev
git clone --branch v0.26 \
    https://github.com/analogdevicesinc/libiio.git
cd libiio

cmake -S . -B build \
    -DPYTHON_BINDINGS=ON \
    -DWITH_USB_BACKEND=ON \
    -DWITH_NETWORK_BACKEND=ON

cmake --build build -j
sudo cmake --install build
```

After installation:

```bash
iio_info --version
iio_info -s
```

If macOS cannot find a library installed under `/usr/local/lib`, try:

```bash
export DYLD_LIBRARY_PATH="/usr/local/lib:$DYLD_LIBRARY_PATH"
```

For Apple Silicon Homebrew libraries, the normal prefix is usually `/opt/homebrew`.

---

## 13. Create the Mac Python environment

Create a project folder:

```bash
mkdir -p ~/two_pluto_video
cd ~/two_pluto_video
```

Copy `pluto_video_rx.py` into this folder.

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install numpy pylibiio pyadi-iio
```

Verify:

```bash
python3 - <<'PY'
import numpy
import iio
import adi

print("NumPy:", numpy.__version__)
print("pylibiio import: OK")
print("pyadi-iio import: OK")
PY
```

Every new Mac terminal should run:

```bash
cd ~/two_pluto_video
source .venv/bin/activate
```

---

## 14. Connect the RX Pluto to the Mac

Connect the Pluto using its USB data/OTG port.

Then:

```bash
iio_info -s
```

Possible contexts include:

```text
usb:1.6.5
ip:pluto.local
ip:192.168.2.1
```

### Recommended URI order on Mac

Try the URI shown by `iio_info -s`.

First preference when available:

```text
usb:<bus.address.interface>
```

Alternative network URI:

```text
ip:pluto.local
```

or:

```text
ip:192.168.2.1
```

Because each Pluto is attached to a different computer, both local machines may use `ip:pluto.local` without conflicting with each other.

Test the selected URI:

```bash
python3 - <<'PY'
import adi

uri = "ip:pluto.local"  # replace if iio_info shows another URI


sdr = adi.Pluto(uri=uri)
print("RX Pluto connected")
print("Sample rate:", sdr.sample_rate)
PY
```

---

# Part C — First two-Pluto test

## 15. Physical setup

Recommended first test:

```text
Windows/WSL + Pluto #1 + TX antenna
                         ~~~ short RF distance ~~~
MacBook + Pluto #2 + RX antenna
```

Start with:

```text
TX gain = -50 dB
RX gain = 20 dB
```

Keep both devices close for the first test, but not with the antennas touching.

---

## 16. Start the Mac receiver first

On Mac:

```bash
cd ~/two_pluto_video
source .venv/bin/activate
```

Run:

```bash
python3 pluto_video_rx.py \
--uri "usb:20.7.5" \
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
--rx-save two_pluto_received.h265
```

Replace `ip:pluto.local` with the actual Mac RX Pluto URI.

The receiver should wait for RF packets.

---

## 17. First file-transfer test from WSL

Before using the camera, test with the known 43 KB HEVC file.

On WSL:

```bash
cd ~/pycode/two_pluto_video
source .venv/bin/activate
```

Run:

```bash
python3 pluto_video_tx.py \
--uri "usb:20.7.5" \
--source file \
--input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--packet-airtime 0.10 \
--inter-packet-gap 0.002 \
--tx-gain -50 \
--tx-save two_pluto_file_tx.h265
```

Replace the TX URI.

Healthy RX output should show:

```text
New TX session: 0x...
RX seq=..., valid=..., duplicates=...
metric=...
CFO=... Hz
rate=... bit/s
```

Normal observations:

- duplicate packets are expected;
- CFO may be several kilohertz because the two Plutos have independent oscillators;
- valid packets should increase;
- skipped packets should remain zero or low.

After TX finishes, stop RX with `Ctrl+C`.

Play the received file on Mac:

```bash
ffplay -f hevc two_pluto_received.h265
```

---

# Part D — Live RTSP video test

## 18. Restart Mac receiver

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
--rx-save two_pluto_live_rx.h265
```

---

## 19. Start WSL RTSP transmitter

Use a conservative 320×180 profile:

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
--tx-save two_pluto_live_tx.h265
```

Use `Ctrl+C` to stop TX and RX.

---

# Part E — Reading the results

## 20. Healthy transmitter output

Example:

```text
TX packet=99,
bytes=40,000,
rate=28,000 bit/s,
source_buffer=8,000 B (~12.8s)
```

The source buffer may rise temporarily, but should not grow forever.

A continuously increasing source buffer means:

```text
encoded video rate > SDR delivery rate
```

Reduce one of:

```text
--video-bitrate
--fps
--video-size
```

or shorten packet airtime only if the RF link is reliable.

---

## 21. Healthy receiver output

Example:

```text
RX seq=99,
valid=100,
duplicates=180,
pending=0,
metric=0.85,
CFO=+12500 Hz,
rate=26,000 bit/s
```

Interpretation:

| Metric | Meaning |
|---|---|
| `valid` | New CRC-valid packets |
| `duplicates` | Repeated cyclic copies; normal |
| `pending` | Out-of-order packets waiting |
| `metric` | Synchronization quality |
| `CFO` | Estimated TX/RX oscillator frequency offset |
| `skipped` | Missing packets abandoned for continuous playback |

Preferred first-test conditions:

```text
valid packets steadily increasing
metric usually > 0.70
CFO reasonably stable
pending usually small
skipped packets zero or low
```

---

# Part F — Tuning

## 22. No packets detected

Try these adjustments one at a time.

Increase TX power gradually:

```text
-50 dB
-45 dB
-40 dB
-35 dB
-30 dB
```

Example:

```bash
--tx-gain -40
```

Increase RX gain:

```bash
--rx-gain 30
```

Lower the synchronization threshold:

```bash
--metric-threshold 0.45
```

Increase packet dwell:

```bash
--packet-airtime 0.15
```

Confirm TX and RX settings match exactly.

---

## 23. Many skipped packets

Increase packet dwell:

```bash
--packet-airtime 0.12
```

Increase RX buffer coverage:

```bash
--rx-buffer-frames 6
```

Increase reorder tolerance:

```bash
--reorder-window 12
--gap-timeout 1.5
```

Increase playback delay:

```bash
--playback-prebuffer-bytes 12000
```

---

## 24. Video works but delay continually grows

The encoded source is too fast.

Reduce bitrate first:

```bash
--video-bitrate 4
```

Then reduce FPS:

```bash
--fps 4
```

Then reduce resolution:

```bash
--video-size 256x144
```

A delay buffer smooths temporary variation. It cannot fix a permanent bitrate deficit.

---

## 25. Increase speed after stability

Change only `--packet-airtime`:

```text
0.10
0.09
0.08
0.07
0.06
```

Test each value for at least several minutes.

Keep the fastest value where:

```text
skipped packets remain zero or acceptably low
video remains continuous
source buffer remains bounded
```

---

## 26. Higher-resolution profiles

### 320×180 recommended baseline

```bash
--video-size 320x180 \
--fps 5 \
--video-bitrate 5
```

### 426×240

```bash
--video-size 426x240 \
--fps 4 \
--video-bitrate 6
```

### 640×360 experimental

```bash
--video-size 640x360 \
--fps 3 \
--video-bitrate 8 \
--keyint-seconds 10
```

The actual encoded bitrate must remain below useful RX throughput.

---

# Part G — Can Mac be transmitter?

## 27. MacBook TX requirements

The Mac can run `pluto_video_tx.py` when all these checks pass:

```bash
python3 -c "import adi, numpy; print('Python SDR stack OK')"
ffmpeg -encoders | grep libx265
iio_info -s
ping 192.168.1.2
```

Test camera access:

```bash
ffmpeg \
-rtsp_transport tcp \
-i "rtsp://admin:PASSWORD@192.168.1.2:554/Preview_01_sub" \
-t 4 \
-map 0:v:0 \
-c:v libx265 \
-f hevc \
-y mac_camera_test.h265
```

When that works, the Mac TX command is the same except for the Mac Pluto URI:

```bash
python3 pluto_video_tx.py \
--uri "ip:pluto.local" \
--source rtsp \
--duration 0 \
--video-size 320x180 \
--fps 5 \
--video-bitrate 5 \
--keyint-seconds 10 \
--encoder-preset veryfast \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--packet-airtime 0.10 \
--inter-packet-gap 0.002 \
--tx-gain -50
```

### Final role recommendation

For the first successful two-computer demonstration:

```text
Windows/WSL = transmitter
MacBook     = receiver
```

After that baseline works, reverse the roles as a portability experiment.

---

# Part H — Useful diagnostics

## 28. List Pluto contexts

WSL or Mac:

```bash
iio_info -s
```

Inspect a particular context:

```bash
iio_info -u "ip:pluto.local"
```

or:

```bash
iio_info -u "usb:1.6.5"
```

---

## 29. Verify Python can open Pluto

```bash
python3 - <<'PY'
import adi

uri = "ip:pluto.local"
sdr = adi.Pluto(uri=uri)

print("Connected:", uri)
print("Sample rate:", sdr.sample_rate)
print("RX LO:", sdr.rx_lo)
print("TX LO:", sdr.tx_lo)
PY
```

---

## 30. Check saved HEVC stream

```bash
ffprobe \
-v error \
-f hevc \
-show_streams \
two_pluto_received.h265
```

Play:

```bash
ffplay -f hevc two_pluto_received.h265
```

---

# Part I — Thesis experiment plan

For each test profile, record:

- computer used for TX;
- computer used for RX;
- Pluto URI;
- frequency;
- sample rate;
- samples per bit;
- payload size;
- packet airtime;
- TX gain;
- RX gain;
- resolution;
- frame rate;
- target encoder bitrate;
- TX useful rate;
- RX useful rate;
- valid packets;
- duplicates;
- skipped packets;
- average synchronization metric;
- average CFO;
- playback delay;
- whether playback remained continuous.

Recommended profile sequence:

```text
1. Known 43 KB HEVC file
2. RTSP 256×144, 4 kbit/s
3. RTSP 320×180, 5 kbit/s
4. Reduce packet airtime
5. RTSP 426×240
6. Experimental 640×360
7. Reverse Mac/WSL roles
```

---

# Part J — References

The setup approach follows these current upstream resources:

- Analog Devices libiio documentation:
  - https://analogdevicesinc.github.io/libiio/main/
  - https://analogdevicesinc.github.io/libiio/main/install/source.html
- Analog Devices PyADI-IIO documentation:
  - https://analogdevicesinc.github.io/pyadi-iio/guides/quick.html
  - https://analogdevicesinc.github.io/documentation/software/pyadi-iio/index.html
- Homebrew:
  - https://brew.sh/
  - https://formulae.brew.sh/formula/ffmpeg
