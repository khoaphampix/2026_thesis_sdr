

python3 one_pluto_live_camera_v8_inplace_resync.py \
--source file \
--copy-original \
--input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
--input-fps 10 \
--payload-size 512 \
--samples-per-bit 4 \
--sample-rate 2000000 \
--captures-per-attempt 6 \
--rx-warmup-captures 1 \
--sync-candidates 16 \
--tx-hold-frames 1.2 \
--minimum-tx-hold 0.015 \
--rx-buffer-frames 3 \
--soft-reset-after 3 \
--hard-reset-after 8 \
--periodic-reset-packets 48 \
--reset-pause 0.15 \
--tx-gain -50 \
--rx-gain 0 \
--retries 20 \
--strict \
--no-display \
--tx-save transmitted_original_v8.h265 \
--rx-save received_original_v8.h265
