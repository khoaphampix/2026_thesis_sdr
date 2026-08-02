# Reliable Two-Pluto RTSP Camera Streaming

Keep `pluto_rtsp_arq_common.py` in the same directory as the local TX or RX
script.

## Start Mac receiver first

```bash
python3 pluto_video_rx_rtsp_arq.py \
--uri "usb:" \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--rx-gain 30 \
--tx-gain -20 \
--ack-airtime 0.25 \
--final-ack-airtime 1.0 \
--turnaround-guard 0.02 \
--candidates-per-phase 16 \
--metric-threshold 0.35 \
--playback-prebuffer-bytes 4000 \
--rx-save two_pluto_camera_received.h265
```

## Start Windows/WSL transmitter second

Edit `CAMERA_URL` in `pluto_video_tx_rtsp_arq.py` and replace only `PASSWORD`.

```bash
python3 pluto_video_tx_rtsp_arq.py \
--uri "usb:" \
--duration 0 \
--video-size 256x144 \
--fps 2 \
--video-bitrate 2 \
--keyint-seconds 20 \
--encoder-preset veryfast \
--frequency 915000000 \
--sample-rate 2000000 \
--samples-per-bit 4 \
--payload-size 400 \
--tx-gain -20 \
--rx-gain 30 \
--data-airtime 0.20 \
--turnaround-guard 0.02 \
--ack-captures 12 \
--post-ack-guard 0.20 \
--retries 30 \
--tx-save two_pluto_camera_tx.h265
```

Stop TX with Ctrl+C. It sends a final size and SHA-256 packet. Leave RX running
until it receives that stop packet and verifies the stream.

If `source_buffer` and `estimated_source_delay` rise continuously, the camera
encoder is producing data faster than the current ARQ radio link can deliver.
Reduce bitrate/FPS/resolution before shortening reliable radio timings.
