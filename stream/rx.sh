# python3 pluto_video_rx_rtsp_arq.py \
# --uri "usb:" \
# --frequency 915000000 \
# --sample-rate 2000000 \
# --samples-per-bit 4 \
# --payload-size 400 \
# --rx-gain 30 \
# --tx-gain -20 \
# --ack-airtime 0.25 \
# --final-ack-airtime 1.0 \
# --turnaround-guard 0.02 \
# --candidates-per-phase 16 \
# --metric-threshold 0.35 \
# --playback-prebuffer-bytes 4000 \
# --rx-save two_pluto_camera_received.h265


# --ack-airtime 0.18 \
# --final-ack-airtime 0.80 \
# --turnaround-guard 0.02



# 320x180.h265
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
--playback-prebuffer-bytes 6000 \
# --playback-prebuffer-bytes 8000 \   # 426x240.h265
# --playback-prebuffer-bytes 12000 \   # 640x360.h265
--rx-save two_pluto_camera_received_320x180.h265




