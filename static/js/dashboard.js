console.log("dashboard.js loaded");
async function updateDashboard() {

    try {

        const response = await fetch("/api/status");

        if (!response.ok)
            throw new Error("Status request failed");

        const s = await response.json();

        // Camera status

        let cameraText = "⚪ Unknown";

        if (s.recording) {

            cameraText = "🔴 Recording";

        } else if (s.boot.ready) {

            cameraText = "🟢 Ready";

        } else {

            cameraText = "🟠 " + s.boot.step;

        }

		document.getElementById("camera-name").textContent =
		    s.camera_name;
		    
        document.getElementById("camera-status").textContent = cameraText;

        document.getElementById("image-count").textContent =
            s.image_count;

        document.getElementById("video-count").textContent =
            s.video_count;

        document.getElementById("motion-count").textContent =
            s.motion_triggers;

        document.getElementById("disk-free").textContent =
            s.disk_free_gb.toFixed(1) + " GB";

        document.getElementById("media-size").textContent =
            s.media_size_mb.toFixed(1) + " MB";



		document.getElementById("last-activity").textContent =

    		s.last_activity || "Never";

		document.getElementById("motion-state").textContent =

    		s.motion_enabled ? "Enabled" : "Disabled";

		document.getElementById("recording-state").textContent =

    		s.recording ? "Recording" : "Idle";            

        // Refresh latest images without browser cache

        const still =
            document.getElementById("last-still");

        if (still && s.last_still) {

            still.src =
                "/media/latest/still?t=" + Date.now();

        }

        const motion =
            document.getElementById("last-motion");

        if (motion && s.last_motion_image) {

            motion.src =
                "/media/latest/motion?t=" + Date.now();

        }

    }

    catch (err) {

        console.error(err);

        document.getElementById("camera-status").textContent =
            "🔴 Offline";

    }

}

updateDashboard();

setInterval(updateDashboard, 3000);
