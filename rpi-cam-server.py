
#!/usr/bin/env python3
import os
import time
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


from flask import (
    Flask,
    jsonify,
    request,
    render_template_string,
    send_from_directory,
    Response,
)

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

import cv2
import numpy as np

# ---------------- Boot status / progress ----------------
from threading import Event
_boot = {"step": "starting", "percent": 0, "ready": False, "errors": []}
_boot_ready_evt = Event()


# ---------------- Camera Manager ----------------

class CameraManager:
    """
    Handles:
      - Always-on preview frames for MJPEG
      - Still capture (from live preview so preview doesn't vanish)
      - 30 s video clips (using FfmpegOutput -> MP4)
      - Optional motion detection that triggers clips
    """

    def __init__(self, base_dir=None):
        self._frame_counter = 0
        self.picam2 = Picamera2()
        self.motion_triggers = 0
        self.video_config = self.picam2.create_video_configuration(

            main={

                "size": (1280, 720),      # recording stream

            },

            lores={

                "size": (320, 240),       # preview stream

                "format": "YUV420",

            },

        )

        self.picam2.configure(self.video_config)
        if base_dir is None:

            base_dir = Path(__file__).resolve().parent / "media"

        self.base_dir = Path(base_dir)

        self.base_dir.mkdir(parents=True, exist_ok=True)  

    # state

        self._lock = threading.Lock()
        self.last_still = None
        self._preview_frame = None
        self.last_motion_image = None
        self._preview_running = False

        self._record_lock = threading.Lock()

        self._recording = False
        self.last_clip = None
        self._motion_enabled = False
        self.motion_area = 1500
        self.motion_frames_required = 3
        self.motion_cooldown = 40

        self._motion_thread = None

        self.last_motion = None
        self._motion_stop_evt = threading.Event()

        self.picam2.start()

        self.start_preview()

    # ---------- Preview ----------

    def start_preview(self):
        with self._lock:
            if self._preview_running:
                return
            self._preview_running = True

        #self.picam2.start()
       # 
        t = threading.Thread(target=self._preview_loop, daemon=True)
        t.start()

    def _preview_loop(self):

        while self._preview_running:
            try:
                print("Before capture")
                yuv = self.picam2.capture_array("lores")  
                print("After capture") 

                frame = cv2.cvtColor(
                    yuv,
                    cv2.COLOR_YUV2BGR_I420
                )

                with self._lock:
                    self._preview_frame = frame.copy()
                self._frame_counter += 1

            except Exception as e:
                print(f"Preview error: {repr(e)}")
                time.sleep(1)

    # ---------- Stills (from preview, no pipeline stop) ----------

    def capture_still(self) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.base_dir / f"still_{ts}.jpg"

        try:
            print("1")
            request = self.picam2.capture_request()
            print("2")
            frame = request.make_array("main")
            print("3")
            request.release()
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            print("4")
            cv2.imwrite(str(path), frame)
            print("5")
            self.last_still = path.name
            return path

        except Exception as e:
            raise RuntimeError(f"Unable to capture still frame: {e}")
    # ---------- 30 s clip ----------

    def start_recording_async(self, duration=30):
        threading.Thread(
            target=self.record_clip,
            args=(duration,),
            daemon=True,
            name="record-thread",
        ).start()


    def record_clip(self, duration: int = 30) -> Optional[Path]:
        """
        Record a clip of `duration` seconds to MP4 via ffmpeg.
        Does not stop the preview.
        """
        with self._record_lock:
            print(f"ENTER record_clip: _recording={self._recording}")
        
            if self._recording:
                print("ABORT record_clip: already recording")
                return None
        
            self._recording = True
            print("SET _recording=True")

        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.base_dir / f"clip_{ts}.mp4"
            print(f"Recording started: {path.name} duration={duration}")

            encoder = H264Encoder(bitrate=5_000_000)
            output = FfmpegOutput(str(path))

        
            self.picam2.start_recording(encoder, output)
            
            time.sleep(duration)
            
            print("STOP_RECORDING_START")
            self.picam2.stop_recording()
            print("STOP_RECORDING_DONE")
            
            print("CAMERA_STOP_START")
            self.picam2.stop()
            print("CAMERA_STOP_DONE")
            
            time.sleep(1)
            
            print("CAMERA_START_START")
            self.picam2.start()
            print("CAMERA_START_DONE")
            
            print(f"Recording finished: {path.name}")
            self.last_clip = path.name
            #encoder.close()   # release V4L2 encoder device
            #output.close()    # close ffmpeg process

            
        except Exception as e:
            print(f"Recording error: {e}")
            
              #  print("Camera restarted")

        finally:
            print("FINALLY reached")
        
            with self._record_lock:
                self._recording = False
                print("SET _recording=False")









    # ---------- Motion detection ----------

    def enable_motion(self):
        self._motion_enabled = True
        if self._motion_thread is None or not self._motion_thread.is_alive():
            self._motion_stop_evt.clear()
            self._motion_thread = threading.Thread(
                target=self._motion_loop, daemon=True
            )
            self._motion_thread.start()

    def disable_motion(self):
        self._motion_enabled = False
        self._motion_stop_evt.set()

    def _motion_loop(self):
        print("enabled")
        prev_gray = None
        cool_down_until = 0
        motion_frame_count = 0

        while not self._motion_stop_evt.is_set():
            with self._lock:
                frame = (
                    None
                    if self._preview_frame is None
                    else self._preview_frame.copy()
                )

            if frame is None:
                time.sleep(0.1)
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            if prev_gray is None:
                prev_gray = gray
                time.sleep(0.1)
                continue

            diff = cv2.absdiff(prev_gray, gray)
            thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(
                thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            motion_detected = any(
                cv2.contourArea(c) > self.motion_area
                for c in contours
            )
            
            if motion_detected:
                motion_frame_count += 1
            else:
                motion_frame_count = 0

            now = time.time()
            if motion_detected:
                print(
                    f"detected now={now:.0f} "
                    f"cooldown={cool_down_until:.0f} "
                    f"recording={self._recording}"
                )
            if (
                motion_frame_count >= self.motion_frames_required
                and now > cool_down_until
            ):
                self.last_motion = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print("recording")
                self.motion_triggers += 1
                # Fire a 30s recording in background
                threading.Thread(
                    target=self.record_clip, args=(10,), daemon=True
                ).start()
    
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                motion_path = (
                    self.base_dir /
                    f"motion_{ts}.jpg"
                )
                
                with self._lock:
                    frame = (
                        None if self._preview_frame is None
                        else self._preview_frame.copy()
                    )
                
                if frame is not None:
                    cv2.imwrite(str(motion_path), frame)
                    self.last_motion_image = motion_path.name

                cool_down_until = now + self.motion_cooldown
                print(f"NEW_COOLDOWN {cool_down_until:.0f}")                       
                    
                    

            #
            prev_gray = gray
            time.sleep(0.1)


# ---------------- Flask app ----------------

app = Flask(__name__)
camera = CameraManager()
_boot.update({"step": "running", "percent": 100, "ready": True})
_boot_ready_evt.set()


INDEX_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>rpi-cam-server</title>
    <style>
      body {
        font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
        margin: 1rem;
        max-width: 800px;
      }
      img {
        max-width: 100%;
        border: 1px solid #ccc;
        border-radius: 4px;
      }
      .controls {
        margin-top: 1rem;
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
      }
      button {
        padding: 0.5rem 1rem;
        cursor: pointer;
        border-radius: 4px;
        border: 1px solid #888;
        background: #f3f3f3;
      }
      button:hover {
        background: #e5e5e5;
      }
      #status {
        margin-top: 1rem;
        font-size: 0.9rem;
      }
      code {
        background: #f5f5f5;
        padding: 0.1rem 0.3rem;
        border-radius: 3px;
      }
    </style>
  </head>
  <body>
    <h1>rpi-cam-server</h1>

    <p>
      Live preview is always on. Stills and 30s clips are saved in
      <code>media/</code> next to this script.
    </p>

    <img id= "live-preview" src="/snapshot.jpg" alt="Live preview" />

    <div class="controls">
      <button id="btn-still">Take still</button>
      <button id="btn-clip">Record 30s clip</button>
      <button id="btn-motion-on">Motion: ON</button>
      <button id="btn-motion-off">Motion: OFF</button>
      <button onclick="window.location='/media/'">
        View Media
      </button>
    </div>

    <div id="status"></div>
    <h3>Latest Still</h3>

    <h3>Storage</h3>
    <div id="storage-summary">
      Loading...
    </div>

    <img id="latest-still"
         src=""
         style="max-width:400px; border:1px solid #ccc;">
    <div style="margin-top:10px;">
      <span id="record-badge"
            style="padding:4px 8px;border-radius:4px;background:#ddd;">
        ⚫ Idle
      </span>
      <span id="motion-badge"
           style="padding:4px 8px;border-radius:4px;background:#ddd;margin-left:10px;">
        ⚪  Motion Off
      </span>
    </div>
    <div id="motion-count" style="margin-top:10px;">
         Motion triggers: 0
    </div>

    <div id="last-motion" style="margin-top:5px;">
        Last motion: Never
    </div>

    <div id="last-clip" style="margin-top:5px;">
        Last clip: None
    </div>

    <h3>Latest Motion</h3>

    <img id="latest-motion"
         src=""
         style="max-width:400px;border:1px solid #ccc;">

    <script>
      function setStatus(msg) {
        document.getElementById("status").textContent = msg;
      }

      async function postJSON(url, data) {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data || {}),
        });
        return res.json();
      }

      document.getElementById("btn-still").onclick = async () => {
        setStatus("Capturing still...");
        try {
          const res = await fetch("/api/capture_still", { method: "POST" });
          const data = await res.json();
          setStatus("Still saved: " + data.file);
        } catch (e) {
          setStatus("Error capturing still");
        }
      };

      document.getElementById("btn-clip").onclick = async () => {
        setStatus("Starting 30s recording...");
        try {
          const data = await postJSON("/api/record_clip", { duration: 30 });
          setStatus(data.message);
        } catch (e) {
          setStatus("Error starting recording");
        }
      };

      document.getElementById("btn-motion-on").onclick = async () => {
        setStatus("Enabling motion detection...");
        try {
          const data = await postJSON("/api/motion", { mode: "on" });
          setStatus("Motion detection: " + data.motion);
        } catch (e) {
          setStatus("Error enabling motion");
        }
      };

      document.getElementById("btn-motion-off").onclick = async () => {
        setStatus("Disabling motion detection...");
        try {
          const data = await postJSON("/api/motion", { mode: "off" });
          setStatus("Motion detection: " + data.motion);
        } catch (e) {
          setStatus("Error disabling motion");
        }
      };
      const livePreview = document.getElementById("live-preview");

    async function updateStatus() {
        try {
            const res = await fetch("/api/status");
            const data = await res.json();

            const recordBadge =
                document.getElementById("record-badge");

            const motionBadge =
                document.getElementById("motion-badge");

            if (data.recording) {
                recordBadge.textContent = "🔴 Recording";
                recordBadge.style.background = "#ffb3b3";
            } else {
                recordBadge.textContent = "🟢 Idle";
                recordBadge.style.background = "#b3ffb3";
            }

            if (data.motion_enabled) {
                motionBadge.textContent = "🟡 Motion Armed";
                motionBadge.style.background = "#fff0b3";
            } else {
                motionBadge.textContent = "⚪ Motion Off";
               motionBadge.style.background = "#ddd";
            }

        document.getElementById("motion-count").textContent =
        "Motion triggers: " + data.motion_triggers;

        document.getElementById("last-motion").textContent =
        "Last motion: " +
        (data.last_motion || "Never");

        document.getElementById("last-clip").textContent =
        "Last clip: " + (data.last_clip || "None");

        document.getElementById("storage-summary").innerHTML =
          "Images: " + data.image_count +
          "<br>Videos: " + data.video_count +
          "<br>Media size: " + data.media_size_mb + " MB" +
          "<br>Disk free: " + data.disk_free_gb + " GB";

    if (data.last_still) {
        document.getElementById("latest-still").src =
        "/media/" + data.last_still;
    }

    if (data.last_motion_image) {
        document.getElementById("latest-motion").src =
        "/media/" + data.last_motion_image +
        "?t=" + Date.now();
     }
        } catch (err) {
            console.log(err);
        }
    }
    setInterval(() => {
        livePreview.src =
            "/snapshot.jpg?t=" + Date.now();
    }, 1000);
    setInterval(updateStatus, 1000);
    updateStatus()
    </script>
  </body>
</html>
"""



@app.route("/")
def index():
    return render_template_string(INDEX_HTML)



@app.route("/api/capture_still", methods=["POST"])
def api_capture_still():
    path = camera.capture_still()
    return jsonify({"status": "ok", "file": path.name})


@app.route("/api/record_clip", methods=["POST"])
def api_record_clip():
    body = request.get_json(silent=True) or {}
    duration = int(body.get("duration", 30))

    camera.start_recording_async(duration)

    return jsonify({
        "status": "recording"
    })

@app.route("/api/motion", methods=["POST"])
def api_motion():
    if request.is_json:
        mode = request.json.get("mode", "off")
    else:
        mode = request.form.get("mode", "off")
    if mode == "on":
        camera.enable_motion()
        return jsonify({"status": "ok", "motion": "on"})
    else:
        camera.disable_motion()
        return jsonify({"status": "ok", "motion": "off"})


@app.route("/media/<path:filename>")
def media_file(filename):
    return send_from_directory(camera.base_dir, filename)


@app.route("/api/status")
def api_status():
    
    images = len(list(camera.base_dir.glob("*.jpg")))
    
    videos = len(list(camera.base_dir.glob("*.mp4")))
    
    media_size = sum(
    
            f.stat().st_size
    
            for f in camera.base_dir.glob("*")
    
            if f.is_file()
    
    )
   
    
    disk = shutil.disk_usage(camera.base_dir)
    return jsonify(
        {
            "boot": _boot,
            "recording": camera._recording,
            "motion_enabled": camera._motion_enabled,
            "motion_triggers": camera.motion_triggers,
            "motion_area": camera.motion_area,
            "motion_frames_required": camera.motion_frames_required,
            "motion_cooldown": camera.motion_cooldown,
            "last_motion": camera.last_motion,
            "last_clip": camera.last_clip,
            "last_still": camera.last_still,
            "last_motion_image": camera.last_motion_image,

            "image_count": images,
            
            "video_count": videos,
            "media_size_mb": round(media_size / 1024 / 1024, 1),
            
            "disk_free_gb": round(disk.free / 1024 / 1024 / 1024, 1),
        }
    )    

@app.route("/snapshot.jpg")
def snapshot():
    with camera._lock:
        if camera._preview_frame is None:
            return ("No frame available", 503)

        frame = camera._preview_frame.copy()

    

    cv2.putText(
        frame,
        datetime.now().strftime("%H:%M:%S"),
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (0, 255, 0),
        2,
    )






    
    ok, jpeg = cv2.imencode(".jpg", frame)

    if not ok:
        return ("JPEG encode failed", 500)
    print("FRAMECOUNT", camera._frame_counter)
    return Response(
        jpeg.tobytes(),
        mimetype="image/jpeg"
    )

@app.route("/media/")
def media_index():
    files = sorted(os.listdir(camera.base_dir))
    items = []
    for f in files:
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".mp4", ".h264")):
            items.append(f)

    html = ["<h1>Media files</h1><ul>"]
    for f in items:
        html.append(f'<li><a href="/media/{f}">{f}</a></li>')
    html.append("</ul>")
    return "".join(html)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
