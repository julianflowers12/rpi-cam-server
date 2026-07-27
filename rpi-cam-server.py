#!/usr/bin/env python3
import os
import time
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from PIL import Image
from collections import OrderedDict
import zipfile
import io
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

VERSION = "1.0"




from flask import (
    Flask,
    jsonify,
    request,
    render_template_string,
    send_from_directory,
    Response,
    redirect,
    render_template,
    send_file,
    url_for
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

from PIL import Image

DEVICE_ID_FILE = Path.home() / ".wildlife-device-id"


def get_device_uuid():

    if DEVICE_ID_FILE.exists():
        return DEVICE_ID_FILE.read_text().strip()

    device_uuid = str(uuid.uuid4())

    DEVICE_ID_FILE.write_text(device_uuid)

    return device_uuid

def create_thumbnail(image_path):
    thumb_dir = image_path.parent / "thumbs"
    thumb_dir.mkdir(exist_ok=True)

    thumb_path = thumb_dir / image_path.name

    img = Image.open(image_path)
    img.thumbnail((320, 180))
    img.save(thumb_path, "JPEG", quality=85)

    return thumb_path

def save_image(image_path, frame):
    """
    Save an image and automatically create its thumbnail.
    """
    cv2.imwrite(str(image_path), frame)
    create_thumbnail(image_path)
    return image_path

def format_timestamp(ts):
    dt = datetime.strptime(ts, "%Y%m%d_%H%M%S")
    return (
        dt.strftime("%d %b %Y"),
        dt.strftime("%H:%M:%S")
    )


def newest_media():

    newest = None
    newest_type = None

    candidates = [

        ("Still", camera.last_still),
        ("Motion", camera.last_motion_image),
        ("Clip", camera.last_clip),

    ]

    for media_type, filename in candidates:

        if not filename:
            continue

        try:

            ts = filename.split("_", 1)[1].split(".")[0]

            dt = datetime.strptime(
                ts,
                "%Y%m%d_%H%M%S"
            )

            if newest is None or dt > newest:

                newest = dt
                newest_type = media_type

        except Exception:
            pass

    return newest, newest_type

def friendly_age(dt):

    if dt is None:
        return "Never"

    seconds = int((datetime.now() - dt).total_seconds())

    if seconds < 60:
        return f"{seconds} sec ago"

    if seconds < 3600:
        return f"{seconds // 60} min ago"

    if seconds < 86400:
        return f"{seconds // 3600} hr ago"

    return f"{seconds // 86400} day(s) ago"        

import subprocess

def create_video_thumbnail(video_path):

    thumb_dir = video_path.parent / "thumbs"
    thumb_dir.mkdir(exist_ok=True)

    thumb_path = thumb_dir / video_path.with_suffix(".jpg").name

    cap = cv2.VideoCapture(str(video_path))

    if not cap.isOpened():
        return thumb_path

    # Jump to 5 seconds
    cap.set(cv2.CAP_PROP_POS_MSEC, 5000)

    ok, frame = cap.read()

    if ok:
        cv2.imwrite(str(thumb_path), frame)

    cap.release()

    return thumb_path
    
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
        self.orientation = 0
        self.motion_triggers = 0
        self._mjpeg_counter = 0
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
        print(self.video_config, flush=True)
        if base_dir is None:

            base_dir = Path(__file__).resolve().parent / "media"

        self.base_dir = Path(base_dir)

        self.base_dir.mkdir(parents=True, exist_ok=True)  

    # state

        self._lock = threading.Lock()
        self._camera_lock = threading.RLock()
        self.last_still = None
        self._preview_frame = None
        self.last_motion_image = None
        self._preview_running = False

        self._record_lock = threading.Lock()

        self._recording = False
        self.last_clip = None
        self._motion_enabled = False
        self.motion_area = 800
        self.motion_frames_required = 2
        self.motion_cooldown = 20

        self._motion_thread = None

        self.last_motion = None
        self._motion_stop_evt = threading.Event()

        # Recover latest media after a reboot
        
        stills = sorted(
            self.base_dir.glob("still_*.jpg"),
            reverse=True
        )
        
        motions = sorted(
            self.base_dir.glob("motion_*.jpg"),
            reverse=True
        )
        
        clips = sorted(
            self.base_dir.glob("clip_*.mp4"),
            reverse=True
        )
        
        if stills:
            self.last_still = str(stills[0].name)
        
        if motions:
            self.last_motion_image = str(motions[0].name)
        
        if clips:
            self.last_clip = str(clips[0].name)

        self.picam2.start()

        self.start_preview()

        self.encoder = H264Encoder(bitrate=5_000_000)

        

    def rotate_video_file(self, video_path, angle):
        """
        Rotate an MP4 file to match self.orientation.
    
        The original file is replaced only after FFmpeg succeeds.
        """
    
        if angle == 0:
            return video_path
    
        video_path = Path(video_path)
        rotated_path = video_path.with_name(
            f"{video_path.stem}_rotating{video_path.suffix}"
        )
    
        if angle == 90:
            video_filter = "transpose=clock"
    
        elif angle == 180:
            video_filter = "hflip,vflip"
    
        elif angle == 270:
            video_filter = "transpose=cclock"
    
        else:
            raise ValueError(
                f"Unsupported orientation: {angle}"
            )
    
        command = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-vf", video_filter,
    
            # Re-encode the video after rotation
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
    
            # Preserve audio if a clip ever contains it
            "-c:a", "copy",
    
            # Improve browser playback
            "-movflags", "+faststart",
    
            str(rotated_path),
        ]
    
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
    
            rotated_path.replace(video_path)
            
            create_video_thumbnail(video_path)
            
            print(
                f"Background rotation finished in {time.time()-t0:.2f}s",
                flush=True
            )
    
            return video_path
    
        except subprocess.CalledProcessError as e:
            rotated_path.unlink(missing_ok=True)
    
            print(
                "Video rotation failed:",
                e.stderr
            )
    
            return video_path
            
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
                with self._camera_lock:
                    raw = self.picam2.capture_array("lores")

                print(
                    f"{self._frame_counter} "
                    f"{raw[0,0]} "
                    f"{int(raw.mean())}"
                )

                # Convert YUV420 -> BGR
                frame = cv2.cvtColor(
                    raw,
                    cv2.COLOR_YUV2BGR_I420
                )

                width = self.video_config["lores"]["size"][0]
                
                height = self.video_config["lores"]["size"][1]
                
                frame = frame[:height, :width]


                # Rotate if required
                if self.orientation == 90:
                    frame = cv2.rotate(
                        frame,
                        cv2.ROTATE_90_CLOCKWISE
                    )

                elif self.orientation == 180:
                    frame = cv2.rotate(
                        frame,
                        cv2.ROTATE_180
                    )

                elif self.orientation == 270:
                    frame = cv2.rotate(
                        frame,
                        cv2.ROTATE_90_COUNTERCLOCKWISE
                    )

                cv2.putText(
                    frame,
                    str(self._frame_counter),
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 255, 0),                        2,
                )
                     

                with self._lock:
                    self._preview_frame = frame.copy()

                self._frame_counter += 1
                
                if self._frame_counter % 100 == 0:
                    print(
                        self._frame_counter,
                        frame.mean()
                    )
                

            except Exception as e:
                print(f"Preview error: {e}")
                time.sleep(1)




    def mjpeg_generator(self):
        while True:
            with self._lock:
                if self._preview_frame is None:
                    frame = None
                else:
                    frame = self._preview_frame.copy()

                    
    
            if frame is None:
                time.sleep(0.05)
                continue

            cv2.putText(
                
                frame,
                
                time.strftime("%H:%M:%S"),
                
                (10, 30),
                
                cv2.FONT_HERSHEY_SIMPLEX,
                
                1,
                
                (0, 255, 0),
                
                2,
                
            )
                
  
    
            ok, jpeg = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), 80]
            )
    
            if not ok:
                continue

            self._mjpeg_counter += 1
                
            if self._mjpeg_counter % 100 == 0:
                print(f"MJPEG {self._mjpeg_counter}", flush=True)    
    
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                + b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                + jpeg.tobytes()
                + b"\r\n"
            )
    
            time.sleep(0.03)      # ~30 fps
                

    # ---------- Stills (from preview, no pipeline stop) ----------

    def capture_still(self) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.base_dir / f"still_{ts}.jpg"

        try:
            with self._camera_lock:
            
                request = self.picam2.capture_request()
                
                frame = request.make_array("main")
            
                request.release()
                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

            if self.orientation == 90:
                frame = cv2.rotate(
                    frame,
                    cv2.ROTATE_90_CLOCKWISE
                )
            
            elif self.orientation == 180:
                frame = cv2.rotate(
                    frame,
                    cv2.ROTATE_180
                )
            
            elif self.orientation == 270:
                frame = cv2.rotate(
                    frame,
                    cv2.ROTATE_90_COUNTERCLOCKWISE
                )
                
            print("4")
            save_image(path, frame)
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

            recording_orientation = self.orientation
        
            if self._recording:
                print("ABORT record_clip: already recording")
                return None
        
            self._recording = True
            print("SET _recording=True")

        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.base_dir / f"clip_{ts}.mp4"
            print(f"Recording started: {path.name} duration={duration}")

            output = FfmpegOutput(str(path))

        
            self.picam2.start_encoder(self.encoder, output)
            
            try:
                time.sleep(duration)
            
            finally: 
                print("STOP_RECORDING_START")

                self.picam2.stop_encoder()

                print("STOP_RECORDING_DONE")

                try:
                    output.close()
                except Exception:
                    pass    
            
            t0 = time.time()
            
            
            
            path = self.rotate_video_file(
                path,
                recording_orientation
            )
            
            print(
                f"Rotate End {time.time() - t0:.2f}s",
                flush=True
            )
            
            print(f"Recording finished: {path.name}")
            
            self.last_clip = path.name
            
            create_video_thumbnail(path)
            
            return path
            
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
                    save_image(motion_path, frame)
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

from flask import Flask, request, url_for

app = Flask(__name__)

GALLERY_FILTERS = (
    "date",
    #"camera",
    #"type",
    #"sort",
    #"page",
)

GALLERY_FILTERS = (
    "date",
    # Add future filters here:
    # "type",
    # "camera",
)


@app.context_processor
def gallery_url_helpers():

    def preserve_gallery_filters(url):
        """
        Add the current gallery filters to an existing URL.

        This is useful because event.video is already a complete URL
        such as /play/clip_20260723_135135.mp4.
        """
        parts = urlsplit(url)

        query = dict(parse_qsl(parts.query))

        for key in GALLERY_FILTERS:
            value = request.args.get(key)

            if value:
                query[key] = value

        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        ))

    def gallery_url(**changes):
        """
        Build a /gallery URL while retaining the current filters.
        """
        values = {
            key: request.args.get(key)
            for key in GALLERY_FILTERS
            if request.args.get(key)
        }

        values.update(changes)

        values = {
            key: value
            for key, value in values.items()
            if value not in (None, "")
        }

        return url_for("gallery", **values)

    return {
        "preserve_gallery_filters": preserve_gallery_filters,
        "gallery_url": gallery_url,
    }


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
      Live preview is always on. Stills and  10s clips are saved in
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
      <button onclick="window.location='/gallery'">
        Gallery
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
          const res = await fetch("/api

          /capture_still", { method: "POST" });
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
    return render_template(
        "index.html",
        title="Garden Wildlife"
    )

@app.route("/api/info")
def api_info():

    return jsonify({

        "uuid": get_device_uuid(),

        "device_type": "camera",

        "hostname": socket.gethostname(),

        "name": socket.gethostname(),

        "version": VERSION,

        "api_version": 1,

        "capabilities": [

            "preview",
            "still",
            "video",
            "motion",
            "gallery"

        ]

    })    


    
@app.route("/preview")
def preview():
    return render_template(
        "preview.html",
        title="Live Preview"
    )


@app.route("/api/capture_still", methods=["POST"])
def api_capture_still():
    path = camera.capture_still()
    return jsonify({"status": "ok", "file": path.name})


@app.route("/stream.mjpg")
def stream_mjpeg():
    return Response(
        camera.mjpeg_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate"
        },
    )

@app.route("/thumbs/<path:filename>")
def thumbnails(filename):
    return send_from_directory(
        camera.base_dir / "thumbs",
        filename
    )

@app.route("/api/media")
def media():
    files = []

    for f in sorted(
        camera.base_dir.glob("still_*.jpg"),
        reverse=True
    ):
        files.append({
            "type": "image",
            "file": f.name,
            "thumb": f"/thumbs/{f.name}"
        })

    return jsonify(files)

def build_media():

    clips = [
        f for f in camera.base_dir.glob("clip_*.mp4")
        if "_rotating" not in f.stem
    ]

    return sorted(
        list(camera.base_dir.glob("still_*.jpg")) +
        list(camera.base_dir.glob("motion_*.jpg")) +
        clips,
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

def media_timestamp(f):

    return (
        f.stem
         .replace("still_", "")
         .replace("motion_", "")
         .replace("clip_", "")
    )
    

@app.route("/api/orientation", methods=["POST"])
def api_orientation():

    body = request.get_json(silent=True) or {}

    print(f"Orientation request body: {body!r}", flush=True)

    try:
        angle = int(body.get("orientation"))
    except (TypeError, ValueError):
        return jsonify({
            "status": "error",
            "message": "Invalid orientation"
        }), 400

    if angle not in (0, 90, 180, 270):
        return jsonify({
            "status": "error",
            "message": "Orientation must be 0, 90, 180 or 270"
        }), 400

    camera.orientation = angle

    print(
        f"API set orientation to {camera.orientation}",
        flush=True
    )

    return jsonify({
        "status": "ok",
        "orientation": camera.orientation
    })
    
def build_events():

    events = []
    used_motion = set()

    # stills

    for f in camera.base_dir.glob("still_*.jpg"):
    

        events.append({
            "type": "still",
            "timestamp": media_timestamp(f),
            "image": f,
            "clip": None,
            "sort": f.stat().st_mtime,
        
        })


    # motion

    for f in camera.base_dir.glob("motion_*.jpg"):

        ts = media_timestamp(f)

        clip = camera.base_dir / f"clip_{ts}.mp4"

        fav = (camera.base_dir / f"{ts}.fav").exists()

        events.append({
            "type": "motion",
            "timestamp": ts,
            "image": f,
            "clip": clip if clip.exists() else None,
            "sort": f.stat().st_mtime,
            "favourite": fav,
        })

        used_motion.add(ts)

    

    for f in camera.base_dir.glob("clip_*.mp4"):
    
        if "_rotating" in f.stem:
            continue
    
        ts = media_timestamp(f)
    
        motion = camera.base_dir / f"motion_{ts}.jpg"
    
        if ts in used_motion:
            continue
    
        thumb = camera.base_dir / "thumbs" / f.with_suffix(".jpg").name
        fav = (camera.base_dir / f"{ts}.fav").exists()
        events.append({
            "type": "clip",
            "timestamp": ts,
            "image": f,
            "clip": f,
            "sort": f.stat().st_mtime,
            "favourite": fav,
        })
        
    events.sort(
        key=lambda e: e["sort"],
        reverse=True
    )
   
    
    return events


def build_groups(events, gap_seconds=90):

    groups = []

    current = None

    for event in events:

        if current is None:

            current = {
                "sort": event["sort"],
                "items": [event]
            }

            groups.append(current)

            continue

        if current["sort"] - event["sort"] <= gap_seconds:

            current["items"].append(event)

        else:

            current = {
                "sort": event["sort"],
                "items": [event]
            }

            groups.append(current)

    return groups    
    
@app.route("/gallery")


def gallery():

    media = build_media()
    events = build_events()
    for index, event in enumerate(events):
        event["index"] = index
    groups = build_groups(events)
    selected_date = request.args.get("date", "")

    print("******** GALLERY CALLED ********")
    app.logger.warning(
        "MEDIA=%d EVENTS=%d GROUPS=%d",
        len(media),
        len(events),
        len(groups),
    )
    available_dates = sorted({
        media_timestamp(f)[:8]
        for f in media
            
    }, reverse=True)

    if selected_date:
    
        groups = [
    
            g for g in groups
    
            if g["items"][0]["timestamp"].startswith(selected_date)
    
        ] 

    for group in groups:

        event = group["items"][0]

    
        image = event["image"]
        clip = event["clip"]
    
        if event["type"] == "still":
    
            event["label"] = "📷 Still Image"
            event["thumb"] = f"/thumbs/{image.name}"
            event["full"] = f"/media/{image.name}"
            event["video"] = (
                url_for(
                    "play_video",
                    filename=clip.name,
                    index=event["index"],
                    date=selected_date or None,
                )
                if clip else None
            )
    
        elif event["type"] == "motion":
    
            event["label"] = "🚶 Wildlife Event"
            event["thumb"] = f"/thumbs/{image.name}"
            event["full"] = f"/media/{image.name}"
    
            
            event["video"] = (
            
                url_for(
            
                    "play_video",
            
                    filename=clip.name,
            
                    index=event["index"],
            
                    date=selected_date or None,
            
                )
            
                if clip else None
            
            )
            
        else:      
    
    
            event["label"] = "🎥 Video clip"
            event["thumb"] = f"/thumbs/{clip.with_suffix('.jpg').name}"
            event["full"] = None
            event["video"] = url_for(
            
                "play_video",
            
                filename=clip.name,
            
                index=event["index"],
            
                date=selected_date or None,
            
            )
    
        event["image_name"] = image.name
        (
        event["date_text"], event["time_text"]) = format_timestamp(
            event["timestamp"]
        )

    return render_template(
        "gallery.html",
        title="Gallery",
        groups=groups,
        available_dates=available_dates,
        selected_date=selected_date,
    )

@app.route("/play/<path:filename>")
def play_video(filename):
    selected_date = request.args.get("date")
    index = request.args.get("index", type=int)
    is_favourite = filename.startswith("fav_")
    events = build_events()

    for i, event in enumerate(events):
        event["index"] = i

    if selected_date:
        back_url = url_for(
            "gallery",
            date=selected_date
        )
    else:
        back_url = url_for("gallery")

    prev_url = None
    next_url = None
    
    if index is not None:
    
        # Find previous event with a clip
        i = index - 1
        while i >= 0:
            clip = events[i]["clip"]
            if clip:
                prev_url = url_for(
                    "play_video",
                    filename=clip.name,
                    index=i,
                    date=selected_date or None,
                )
                break
            i -= 1
    
        # Find next event with a clip
        i = index + 1
        while i < len(events):
            clip = events[i]["clip"]
            if clip:
                next_url = url_for(
                    "play_video",
                    filename=clip.name,
                    index=i,
                    date=selected_date or None,
                )
                break
            i += 1
        
    print(f"index={index}")
    print(f"prev_url={prev_url}")
    print(f"next_url={next_url}")      

    print("========== PLAY ==========")
    print("filename =", filename)
    print("index =", index)
    print("prev_url =", prev_url)
    print("next_url =", next_url)

    print("==========================")    

    return render_template(
        "play.html",
        filename=filename,
        back_url=back_url,
        prev_url=prev_url,
        next_url=next_url,
        is_favourite=is_favourite,
    )

@app.route("/favourite/<path:filename>", methods=["POST"])
def favourite_event(filename):

    clip = camera.base_dir / filename

    if not clip.exists():
        abort(404)

    ts = clip.stem.replace("clip_", "").replace("fav_clip_", "")

    files = [
        camera.base_dir / f"clip_{ts}.mp4",
        camera.base_dir / f"motion_{ts}.jpg",
        camera.base_dir / "thumbs" / f"clip_{ts}.jpg",
    ]

    for path in files:

        if not path.exists():
            continue

        new_name = "fav_" + path.name
        path.rename(path.with_name(new_name))

    return ("", 204)    
    
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

@app.route("/delete/<filename>", methods=["POST"])
def delete_media(filename):

    path = camera.base_dir / filename

    if not path.exists():
        abort(404)

    thumbs_dir = camera.base_dir / "thumbs"    

    # Delete matching clip if this is a motion image
    if filename.startswith("motion_"):

        ts = media_timestamp(path)

        clip = camera.base_dir / f"clip_{ts}.mp4"

        motion_thumb = thumbs_dir / filename
        
        if motion_thumb.exists():
        
            motion_thumb.unlink()
        
        clip_thumb = thumbs_dir / f"clip_{ts}.jpg"
        
        if clip_thumb.exists():
            clip_thumb.unlink()

        if clip.exists():
            clip.unlink()

        elif filename.startswith("still_"):
        
            still_thumb = thumbs_dir / filename
        
            if still_thumb.exists():
        
                still_thumb.unlink()
    path.unlink()

    return redirect("/gallery")
        
@app.route("/delete-selected/", methods=["POST"])
def delete_selected():

    selected = request.form.getlist("selected")

    thumbs_dir = camera.base_dir / "thumbs"

    for filename in selected:

        path = camera.base_dir / filename

        if not path.exists():
            continue

        if filename.startswith("motion_"):

            ts = media_timestamp(path)

            clip = camera.base_dir / f"clip_{ts}.mp4"

            thumb = thumbs_dir / filename

            if thumb.exists():
                thumb.unlink()

            clip_thumb = thumbs_dir / f"clip_{ts}.jpg"

            if clip_thumb.exists():
                clip_thumb.unlink()

            if clip.exists():
                clip.unlink()

        elif filename.startswith("still_"):

            thumb = thumbs_dir / filename

            if thumb.exists():
                thumb.unlink()

        path.unlink()

    return redirect("/gallery")

@app.route("/download-selected", methods=["POST"])
def download_selected():

    print(request.form)
    

    selected = request.form.getlist("selected")

    memory_file = io.BytesIO()

    with zipfile.ZipFile(
        memory_file,
        "w",
        zipfile.ZIP_DEFLATED
    ) as zf:

        for filename in selected:

            path = camera.base_dir / filename

            if path.exists():
                zf.write(path, arcname=filename)

            # include matching clip automatically
            if filename.startswith("motion_"):

                ts = media_timestamp(path)

                clip = camera.base_dir / f"clip_{ts}.mp4"

                if clip.exists():
                    zf.write(
                        clip,
                        arcname=clip.name
                    )

    memory_file.seek(0)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"selected_{stamp}.zip",
    ) 

@app.route("/download-all")
def download_all():

    memory_file = io.BytesIO()

    with zipfile.ZipFile(
        memory_file,
        "w",
        zipfile.ZIP_DEFLATED
    ) as zf:

        for f in build_media():
            zf.write(f, arcname=f.name)

    memory_file.seek(0)

    return send_file(
        memory_file,
        as_attachment=True,
        download_name=(
            f"wildlife_{datetime.now():%Y%m%d_%H%M%S}.zip"
        ),
        mimetype="application/zip",
    )   

@app.route("/api/status")
def api_status():
    
    images = len(list(camera.base_dir.glob("*.jpg")))
    
    videos = len(list(camera.base_dir.glob("*.mp4")))
    
    media_size = sum(
    
            f.stat().st_size
    
            for f in camera.base_dir.glob("*")
    
            if f.is_file()
    
    )

    latest_dt, latest_type = newest_media()
   
    
    disk = shutil.disk_usage(camera.base_dir)

    status = {
        "boot": _boot,
        "recording": camera._recording,
        "camera_online": True,
        "camera_name": 'Feeder',
        "motion_enabled": camera._motion_enabled,
        "motion_triggers": camera.motion_triggers,
        "motion_area": camera.motion_area,
        "motion_frames_required": camera.motion_frames_required,
        "motion_cooldown": camera.motion_cooldown,
        "last_motion": camera.last_motion,
        "last_still": camera.last_still if camera.last_still else None,
        "last_motion_image": camera.last_motion_image if camera.last_motion_image else None,
        "last_clip": camera.last_clip if camera.last_clip else None,
        "image_count": images,
        "video_count": videos,
        "media_size_mb": round(media_size / 1024 / 1024, 1),
        "disk_free_gb": round(disk.free / 1024 / 1024 / 1024, 1),
        "last_activity": friendly_age(latest_dt),
        "orientation": camera.orientation,
        "last_actvity_type": latest_type,
        
    }
   
    for key, value in status.items():
        print(f"{key}: {type(value)}")
        if isinstance(value, dict):
            for k, v in value.items():
                print(f"    {k}: {type(v)}")
   
    return jsonify(status)

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


@app.route("/media/latest/still")
def latest_still():

    if camera.last_still is None:
        abort(404)

    return send_from_directory(
        camera.base_dir,
        camera.last_still
    )


@app.route("/media/latest/motion")
def latest_motion():

    if camera.last_motion_image is None:
        abort(404)

    return send_from_directory(
        camera.base_dir,
        camera.last_motion_image
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
