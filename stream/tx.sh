# python3 pluto_video_tx_rtsp_arq.py \
# --uri "usb:" \
# --duration 60 \
# --video-size 256x144 \
# --fps 2 \
# --video-bitrate 2 \
# --keyint-seconds 20 \
# --encoder-preset veryfast \
# --frequency 915000000 \
# --sample-rate 2000000 \
# --samples-per-bit 4 \
# --payload-size 400 \
# --tx-gain -20 \
# --rx-gain 30 \
# --data-airtime 0.20 \
# --turnaround-guard 0.02 \
# --ack-captures 12 \
# --post-ack-guard 0.20 \
# --retries 30 \
# --tx-save two_pluto_camera_tx.h265


# --ack-airtime 0.18 \
# --final-ack-airtime 0.80 \
# --turnaround-guard 0.02


python3 pluto_video_tx_rtsp_arq.py \
--uri "usb:" \
--duration 60 \
--video-size 320x180 \
--fps 2 \
--video-bitrate 3 \
--keyint-seconds 15 \
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
--tx-save two_pluto_camera_tx_320x180.h265



# 426x240

# python3 pluto_video_tx_rtsp_arq.py \
# --uri "usb:" \
# --duration 60 \
# --video-size 426x240 \
# --fps 2 \
# --video-bitrate 4 \
# --keyint-seconds 15 \
# --encoder-preset veryfast \
# --frequency 915000000 \
# --sample-rate 2000000 \
# --samples-per-bit 4 \
# --payload-size 400 \
# --tx-gain -20 \
# --rx-gain 30 \
# --data-airtime 0.20 \
# --turnaround-guard 0.02 \
# --ack-captures 12 \
# --post-ack-guard 0.20 \
# --retries 30 \
# --tx-save two_pluto_camera_tx_426x240.h265


# 640x360

# python3 pluto_video_tx_rtsp_arq.py \
# --uri "usb:" \
# --duration 60 \
# --video-size 640x360 \
# --fps 1 \
# --video-bitrate 4 \
# --keyint-seconds 15 \
# --encoder-preset faster \
# --frequency 915000000 \
# --sample-rate 2000000 \
# --samples-per-bit 4 \
# --payload-size 400 \
# --tx-gain -20 \
# --rx-gain 30 \
# --data-airtime 0.20 \
# --turnaround-guard 0.02 \
# --ack-captures 12 \
# --post-ack-guard 0.20 \
# --retries 30 \
# --tx-save two_pluto_camera_tx_640x360.h265