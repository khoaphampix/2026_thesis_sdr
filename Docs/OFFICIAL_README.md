# window env
--------------- device 2 --------------- WINDOW - dark tape
usbipd list
usbipd unbind --hardware-id 0456:b673
usbipd detach --hardware-id 0456:b673


usbipd bind --hardware-id 0456:b673
usbipd attach --wsl --hardware-id 0456:b673
usbipd bind --hardware-id 0456:b673 & usbipd attach --wsl --hardware-id 0456:b673
usbipd deattach --wsl --hardware-id 0456:b673 & usbipd unbind --hardware-id 0456:b673


--------------- device 1 ---------------
usbipd list
usbipd unbind --hardware-id 0456:b673
usbipd detach --hardware-id 0456:b673


usbipd bind --hardware-id 0456:b673
usbipd attach --wsl --hardware-id 0456:b673


usbipd bind --hardware-id 0456:b673 && usbipd attach --wsl --hardware-id 0456:b673
OR 
usbipd bind --busid <YOUR-BUS-ID>
usbipd attach --wsl --busid <YOUR-BUS-ID>

wsl --list --verbose
wsl -d Ubuntu-22.04

# ubuntu env
cd ~/pycode


python3 pluto_bit_file_tx_rx.py \
    input_video_football_cif_yuv_10.bit 


python3 pluto_bit_file_tx_rx.py \
    input_video_football_cif_yuv_10.bit \
    --output received_test_football_cif_yuv_10.bit


python3 pluto_bit_file_tx_rx.py \
     ../input_encoded_files/Netflix_Crosswalk_4096x2160_60fps_10bit_420.bit\
       --output  ../output_encoded_transmited_files/received_video_Netflix_Crosswalk_4096x2160_60fps_10bit_420_one_file_code.bit




python3 main_bpsk_video_file.py \
     ../input_encoded_files/input_video_football_cif_yuv_10.bit\
    ../output_encoded_transmited_files/received_video_football_cif_yuv_10.bit

python3 main_bpsk_video_file.py \
     ../input_encoded_files/Netflix_Crosswalk_4096x2160_60fps_10bit_420.bit\
    ../output_encoded_transmited_files/received_video_Netflix_Crosswalk_4096x2160_60fps_10bit_420_one_file_code.bit

----------------------
python3 main_bpsk_video_file.py \
    ../input_encoded_files/Netflix_Crosswalk_4096x2160_60fps_10bit_420.bit \
    ../output_encoded_transmited_files/received_video_Netflix_Crosswalk_4096x2160_60fps_10bit_420.bit

--------------------

python3 simulate_camera_pluto_loopback.py \
    ../input_encoded_files/Netflix_Crosswalk_4096x2160_60fps_10bit_420.bit \
    ../output_encoded_transmited_files/received_stream.bit \
    --camera-bitrate 80000


python3 simulate_camera_pluto_loopback.py \
    ../input_encoded_files/input_video_football_cif_yuv_10.bit \
    ../output_encoded_input_video_football_cif_yuv_10.bit \
    --camera-bitrate 80000

python3 simulate_camera_pluto_loopback.py \
    ../input_encoded_files/input_video_football_cif_yuv_10.bit \
    ../output_encoded_transmited_files/received_stream.bit \
    --camera-bitrate 80000


python3 pluto_stream_rx.py ../input_encoded_files/input_video_football_cif_yuv_10.bit
python3 pluto_stream_rx.py received_live.bit



python3 pluto_stream_tx.py \
    ../input_encoded_files/input_video_football_cif_yuv_10.bit \
    --camera-bitrate 80000




     ffmpeg -f rawvideo -pixel_format yuv420p -video_size 352x288 -framerate 30 -i "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Input\akiyo_cif.y4m" -c:v libx265 -preset medium -crf 28 -x265-params "repeat-headers=1" -f hevc output_h265.bit



ffplay -f hevc -framerate 60 "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\h265_output\output_h265.bit"

ffplay -f vvc -framerate 60 "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Output\encoding logs\Netflix_BoxingPractice_4096x2160_60fps_10bit_420_y4m_frame_100\Netflix_BoxingPractice_4096x2160_60fps_10bit_420_y4m_frame_100_fr_new.bit"

ffplay -f vvc -framerate 60 "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Output\encoding logs\Netflix_Narrator_4096x2160_60fps_10bit_420_y4m_frame_100\Netflix_Narrator_4096x2160_60fps_10bit_420_y4m_frame_100.bit"



ffplay -f vvc -framerate 60 "received_stream.bit"ls -

 ffmpeg -f rawvideo -pixel_format yuv422p -video_size 720x486 -framerate 60 -i  "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Input\akiyo_cif.y4m" -c:v libx265 -preset medium -crf 28 -x265-params "repeat-headers=1" -f hevc output_h265.bit


 ffplay -f hevc -framerate 60 "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\h265_output\output_h265.bit"


ffplay "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Input\foreman_qcif_mono.y4m"


 ffplay -f hevc -framerate 60  "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Output\encoding logs\hevcsamples\rush_hour_4_frm.bin"
 
ffmpeg -i "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Input\tempete_cif.y4m" -c:v libx265 -crf 28 -bsf:v hevc_mp4toannexb "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Output\encoding logs\hevcsamples\tempete_cif.h265"


 ffplay -f hevc -framerate 60  "C:\Users\User\OneDrive - Charles Darwin University\Semester 3\Thesis part A\Working\Testing BitFrame\Output\encoding logs\hevcsamples\tempete_cif.h265"



 ffmpeg -rtsp_transport tcp \
-i "$CAMERA_URL" \
-t 2 \
-map 0:v:0 \
-an \
-c:v copy \
-f hevc \
-y ~/camera_test/camera_stream.h265


ffplay -f hevc -framerate 60  "/home/kev/camera_test/camera_stream.h265"
ffplay -f hevc -framerate 60  "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/transmitted_camera.h265"
ffplay -f hevc -framerate 60  "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265"

cp  "/home/kev/camera_test/camera_stream.h265" "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265"

/home/kev/pycode/one_pluto_live_camera_v3_file_fix

sdr stream video reolink

python3 one_pluto_live_camera_v3_file_fix.py \
  --source file \
  --input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
  --input-fps 30 \
  --duration 20 \
  --video-size 176x144 \
  --fps 2 \
  --video-bitrate 6 \
  --samples-per-bit 4 \
  --payload-size 256 \
  --tx-gain -50 \
  --rx-gain 0

-----------------------------------------------
with 265 full 

python3 /home/kev/pycode/one_pluto_live_camera_v3_file_fix/one_pluto_live_camera_v3_file_fix.py \
  --source file \
  --input "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265" \
  --input-fps 30 \
  --duration 20 \
  --video-size 128x72 \
  --fps 3 \
  --video-bitrate 4 \
  --samples-per-bit 4 \
  --payload-size 256 \
  --tx-gain -50 \
  --rx-gain 0

/home/kev/pycode/one_pluto_live_camera_v3_file_fix

ffplay -f hevc -framerate 60  "/home/kev/camera_test/camera_stream.h265"

.h265
ffplay -f hevc -framerate 60  "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/received_original_v8.h265"
ffplay -f hevc -framerate 60  "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/received_v6_baseline.h265"
ffplay -f hevc -framerate 60  "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265"

cp  "/home/kev/camera_test/camera_stream.h265" "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265"

CAMERA_URL='rtsp://admin:cdu_2026@192.168.1.2:554/Preview_01_sub'
ffmpeg -rtsp_transport tcp \
-i "$CAMERA_URL" \
-t 30 \
-map 0:v:0 \
-c:v copy \
-f hevc \
-y ~/camera_test/camera_stream.h265


cp  "/home/kev/camera_test/camera_stream.h265" "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265"

cp  "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/received_real_v8_6.h265"  "/home/kev/camera_test/camera_stream.h265"



received_real_v8_6.h265
ffplay -f hevc "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/transmitted_rtsp_320x180.h265"


ffplay -f hevc "/home/kev/camera_test/camera_stream.h265"

ffplay -f hevc "/home/kev/pycode/one_pluto_live_camera_v3_file_fix/camera_stream_transmitted_camera.h265"


ffplay -f hevc -framerate 60  "two_pluto_received_exact.h265"

CAMERA_URL='rtsp://admin:cdu_2026@192.168.1.2:554/Preview_01_sub'

ffmpeg -rtsp_transport tcp \
-i "$CAMERA_URL" \
-t 30 \
-map 0:v:0 \
-c:v libx265 \
-f hevc \
-y ~/camera_test/camera_stream.h265



codec_name=hevc
width=640
height=360
r_frame_rate=10/1

---------------------------- with converter ---------------

CAMERA_URL='rtsp://admin:PASSWORD@192.168.1.2:554/Preview_01_sub'

ffmpeg -rtsp_transport tcp \
-i "$CAMERA_URL" \
-t 10 \
-map 0:v:0 \
-an \
-vf "fps=3,scale=160:90" \
-c:v libx265 \
-preset ultrafast \
-tune zerolatency \
-b:v 4k \
-maxrate 4k \
-bufsize 8k \
-pix_fmt yuv420p \
-x265-params "keyint=3:min-keyint=3:scenecut=0:bframes=0:repeat-headers=1:aud=1" \
-f hevc \
-y ~/camera_test/camera_sdr_4k.h265



ffplay "rtsp://admin:cdu_2026@192.168.1.2:554/Preview_01_sub"

CAMERA_URL='rtsp://admin:cdu_2026@192.168.1.2:554/Preview_01_sub'

ffmpeg -rtsp_transport tcp \
-i "$CAMERA_URL" \
-t 10 \
-map 0:v:0 \
-an \
-vf "scale=160:120,fps=3" \
-c:v libx265 \
-preset ultrafast \
-tune zerolatency \
-pix_fmt yuv420p \
-b:v 4k \
-maxrate 4k \
-bufsize 8k \
-x265-params "keyint=3:min-keyint=3:scenecut=0:bframes=0:repeat-headers=1:aud=1" \
-f hevc \
-y ~/camera_test/camera_sdr.h265

-------------------------------------------

python3 adi_mac.py \
--uri "usb:" \
--frequency 915000000 \
--sample-rate 2000000 \
--tone-offset 200000 \
--rx-gain 20 \
--snr-threshold 18

python3 adi_win.py \
--uri "usb:" \
--frequency 915000000 \
--sample-rate 2000000 \
--tone-offset 200000 \
--tx-gain -40
