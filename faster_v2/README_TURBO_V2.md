# Two-Pluto Turbo File ARQ v2

This is the next speed step after the working fast-window v1. It remains an exact file-transfer test: the receiver reconstructs packets by sequence number and verifies final SHA-256.

## Main changes

- BPSK sample rate: 8 MS/s
- 4 samples/bit = 2 Mbit/s gross BPSK
- payload: 1200 bytes
- selective-repeat window: 12 packets
- preamble: 256 bits
- TX data burst hold is derived from actual IQ burst length
- selective retransmission bursts use shorter airtime automatically
- bitmap ACK airtime: 8 ms
- RX can send a bitmap early after a quiet interval, which accelerates retries

Keep `pluto_fast_arq_common_v2.py` in the same folder as the TX/RX file.

## Mac RX first

```bash
python3 pluto_video_rx_fast_arq_v2.py \
--uri "usb:" \
--frequency 915000000 \
--sample-rate 8000000 \
--samples-per-bit 4 \
--payload-size 1200 \
--window-size 12 \
--rx-gain 30 \
--tx-gain -20 \
--rx-buffer-size 131072 \
--ack-airtime 0.008 \
--final-ack-airtime 0.030 \
--turnaround-guard 0.002 \
--ack-delay-factor 0.90 \
--ack-idle-seconds 0.030 \
--control-ack-delay 0.035 \
--metric-threshold 0.35 \
--candidates-per-phase 8 \
--rx-save two_pluto_received_turbo_v2.h265
```

## Windows/WSL TX second

```bash
python3 pluto_video_tx_fast_arq_v2.py \
--uri "usb:" \
--input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
--frequency 915000000 \
--sample-rate 8000000 \
--samples-per-bit 4 \
--payload-size 1200 \
--window-size 12 \
--tx-gain -20 \
--rx-gain 30 \
--data-slot 0 \
--burst-repeat-factor 1.10 \
--control-slot 0.030 \
--turnaround-guard 0.002 \
--ack-captures 24 \
--ack-rx-buffer 8192 \
--retries 15 \
--metric-threshold 0.35 \
--candidates-per-phase 8
```

If v2 is not reliable at 8 MS/s, keep all v2 code but test `--sample-rate 6000000` on both computers first.
