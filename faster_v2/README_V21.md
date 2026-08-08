# Stable-Fast Window ARQ v2.1

This version is intentionally based on the hardware-proven v1, not the failed
8 MS/s turbo preset.

Main changes:
- 512-bit preamble retained for robustness.
- 4 MS/s default retained.
- 1000-byte payload.
- 8-packet window.
- IQ for every data packet is precomputed once.
- Retransmissions concatenate cached IQ only.
- Burst hold time follows actual retransmission burst length.
- 2 ms turnaround.
- 30 ms bitmap ACK airtime with earlier ACK scheduling.
- No disk flush after every packet.
- Exact final size and SHA-256 are still required.

## Mac RX first

```bash
python3 pluto_video_rx_fast_arq_v21.py \
--uri "usb:" \
--frequency 915000000 \
--sample-rate 4000000 \
--samples-per-bit 4 \
--payload-size 1000 \
--window-size 8 \
--rx-gain 30 \
--tx-gain -20 \
--rx-buffer-size 65536 \
--ack-airtime 0.030 \
--final-ack-airtime 0.060 \
--turnaround-guard 0.002 \
--ack-delay-factor 0.65 \
--control-ack-delay 0.025 \
--metric-threshold 0.35 \
--candidates-per-phase 8 \
--rx-save two_pluto_received_v21.h265
```

## Windows/WSL TX

```bash
python3 pluto_video_tx_fast_arq_v21.py \
--uri "usb:" \
--input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
--frequency 915000000 \
--sample-rate 4000000 \
--samples-per-bit 4 \
--payload-size 1000 \
--window-size 8 \
--tx-gain -20 \
--rx-gain 30 \
--data-slot 0 \
--burst-repeat-factor 1.20 \
--control-slot 0.050 \
--turnaround-guard 0.002 \
--ack-captures 24 \
--ack-rx-buffer 8192 \
--retries 20 \
--metric-threshold 0.35 \
--candidates-per-phase 8
```

After this passes SHA, test the exact same commands with `--sample-rate 6000000`
on BOTH computers. Keep every other parameter unchanged first.
