python3 pluto_video_tx_arq.py \
--uri "usb:" \
--input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
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
--retries 30

