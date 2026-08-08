# Fast two-Pluto exact file transfer

This version replaces per-packet stop-and-wait ARQ with windowed selective-repeat ARQ.

## Files

Keep `pluto_fast_arq_common.py` in the same directory as the TX/RX script.

## 1. Mac receiver first

```bash
python3 pluto_video_rx_fast_arq.py \
--uri "usb:" \
--frequency 915000000 \
--sample-rate 4000000 \
--samples-per-bit 4 \
--payload-size 800 \
--window-size 8 \
--rx-gain 30 \
--tx-gain -20 \
--rx-buffer-size 65536 \
--ack-airtime 0.020 \
--final-ack-airtime 0.080 \
--turnaround-guard 0.005 \
--ack-delay-factor 0.95 \
--metric-threshold 0.35 \
--candidates-per-phase 8 \
--rx-save two_pluto_received_fast_exact.h265
```

## 2. Windows/WSL transmitter second

```bash
python3 pluto_video_tx_fast_arq.py \
--uri "usb:" \
--input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
--frequency 915000000 \
--sample-rate 4000000 \
--samples-per-bit 4 \
--payload-size 800 \
--window-size 8 \
--tx-gain -20 \
--rx-gain 30 \
--data-slot 0.10 \
--control-slot 0.08 \
--turnaround-guard 0.005 \
--ack-captures 40 \
--ack-rx-buffer 16384 \
--retries 20 \
--metric-threshold 0.35 \
--candidates-per-phase 8
```

## What to expect

TX sends up to eight data packets in one RF superframe and receives one bitmap ACK.
If only packets 3 and 6 were missed, the next burst contains only those missing packets.

Successful RX ends with:

```text
Packet count match: True
Size match: True
Hash match: True
RESULT: TX and RX files are identical.
```

## If the first 4 MS/s test is unstable

Keep the new windowed protocol but temporarily use:

```text
sample-rate 2000000
payload-size 600
window-size 6
```

on both computers.

## After the baseline succeeds

Try these one at a time:

1. `--payload-size 1000` on both TX and RX.
2. `--window-size 10` or `12` on both sides.
3. Reduce `--data-slot` to `0.08`; TX automatically raises it if the RF burst physically needs longer.
4. Reduce ACK airtime gradually from `0.020` toward `0.010`.

Always compare the final SHA-256 before keeping a faster profile.
