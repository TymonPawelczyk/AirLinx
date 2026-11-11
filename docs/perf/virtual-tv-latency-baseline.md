# Virtual TV: sender -> receiver latency baseline

Measured by `tests/virtual_tv/test_latency.py` against the virtual TV on this host (x86_64, 16 cores, Python 3.10.12, 2026-11-11). One-way delay on a single clock: *time the frame was complete at the receiver* minus *the instant the sender stamped on it* (RTP timestamp mapped through the RTCP Sender Report). Per-frame, 10 s of steady state per row, first 20% dropped.

**Not included:** screen capture and encoding (the stamp is the sender's own capture-time estimate), Wi-Fi, and everything inside a real TV (jitter buffer of `targetDelay`, hardware decode, display). This is the floor under the end-to-end latency, with no TV decoder in the loop.

| Encoder | Output | Stream | frames | p50 ms | p90 ms | p99 ms | max ms | stdev ms |
|---|---|---|---:|---:|---:|---:|---:|---:|
| libx264 | 1280x720@60 8 Mb/s | video | 481 | 9.3 | 13.2 | 15.5 | 19.9 | 3.0 |
| libx264 | 1280x720@60 8 Mb/s | audio | 801 | 33.6 | 42.4 | 46.8 | 48.1 | 6.6 |
| libx264 | 1920x1080@60 12 Mb/s | video | 482 | 10.3 | 14.3 | 16.1 | 18.7 | 3.1 |
| libx264 | 1920x1080@60 12 Mb/s | audio | 801 | 33.7 | 42.5 | 46.8 | 48.5 | 6.6 |
| h264_nvenc + CPU scale | 1920x1080@60 12 Mb/s | video | 492 | 9.5 | 13.8 | 15.7 | 16.2 | 3.0 |
| h264_nvenc + CPU scale | 1920x1080@60 12 Mb/s | audio | 819 | 33.5 | 42.3 | 46.1 | 48.2 | 6.6 |

For comparison, the sender's own estimate shown in the UI for the same runs (`capture + encode + network + tv`; its `tv` term is the *assumed* 100 ms `targetDelay`, not measured):

- libx264 1280x720: 111 ms total = capture 11.3 + encode 8.8 + network 0.3 + tv 90.9
- libx264 1920x1080: 111 ms total = capture 11.3 + encode 9.4 + network 1.5 + tv 89.1
- h264_nvenc + CPU scale 1920x1080: 111 ms total = capture 11.3 + encode 8.6 + network 0.4 + tv 90.9

Reading the table: video p50 of ~9 ms is packetization, pacing and loopback; the audio figure is larger because the sender back-dates audio by an assumed 20 ms Pulse/Opus fragment and packets are 10 ms long. The p99/max columns show scheduling jitter of the sender process, the number to watch for regressions (for example a pacer change that bursts keyframes).
