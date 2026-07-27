from picamera2 import Picamera2
from gpiozero import Button
from flask import Flask, Response, jsonify, send_file
from datetime import datetime
import cv2
import time
import os
import threading


# ---------- Settings ----------
touchSensorPin = 17
photoFolder = "/home/pi/smileTouchProject/photos"
videoFolder = "/home/pi/smileTouchProject/videos"
cameraWidth = 1280
cameraHeight = 720
videoFps = 15
smileCooldownSeconds = 3
useReleasedTrigger = True


# ---------- Folders ----------
os.makedirs(photoFolder, exist_ok=True)
os.makedirs(videoFolder, exist_ok=True)


# ---------- Shared State ----------
app = Flask(__name__)
latestJpegFrame = None
latestPhotoPath = None
latestVideoPath = None
latestEvent = "Program starting..."
isRecording = False
frameLock = threading.Lock()
stateLock = threading.Lock()
videoWriter = None
currentVideoPath = None
lastSmilePhotoTime = 0


# ---------- Website Routes ----------
@app.route("/")
def home():
    return """
    <html><head><title>Raspberry Pi Camera</title></head>
    <body style="background-color:#111; color:white; text-align:center; font-family:Arial;">
        <h1>Raspberry Pi Live Camera</h1>
        <img src="/video" style="width:90%; max-width:1000px; border:3px solid white;">
        <h2>Status</h2>
        <p id="event">Loading...</p>
        <p id="recording">Recording: Loading...</p>
        <h2>Latest Files</h2>
        <p id="photo">Latest photo: Loading...</p>
        <p id="videoFile">Latest video: Loading...</p>
        <a href="/latest-photo">Download Latest Photo</a><br>
        <a href="/latest-video">Download Latest Video</a>
        <script>
            async function updateStatus() {
                const response = await fetch('/status');
                const data = await response.json();
                document.getElementById('event').innerText = data.latestEvent;
                document.getElementById('recording').innerText =
                    'Recording: ' + (data.isRecording ? 'YES' : 'NO');
                document.getElementById('photo').innerText =
                    'Latest photo: ' + (data.latestPhoto || 'None yet');
                document.getElementById('videoFile').innerText =
                    'Latest video: ' + (data.latestVideo || 'None yet');
            }
            setInterval(updateStatus, 1000);
            updateStatus();
        </script>
    </body></html>
    """


@app.route("/video")
def video():
    return Response(
        generateFrames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/status")
def status():
    with stateLock:
        return jsonify(
            {
                "latestEvent": latestEvent,
                "isRecording": isRecording,
                "latestPhoto": (
                    os.path.basename(latestPhotoPath)
                    if latestPhotoPath
                    else None
                ),
                "latestVideo": (
                    os.path.basename(latestVideoPath)
                    if latestVideoPath
                    else None
                ),
            }
        )


@app.route("/latest-photo")
def downloadLatestPhoto():
    with stateLock:
        path = latestPhotoPath
    if path is None or not os.path.exists(path):
        return "No photo saved yet.", 404
    return send_file(path, as_attachment=True)


@app.route("/latest-video")
def downloadLatestVideo():
    with stateLock:
        path = latestVideoPath
    if path is None or not os.path.exists(path):
        return "No video saved yet.", 404
    return send_file(path, as_attachment=True)


def generateFrames():
    while True:
        with frameLock:
            frame = latestJpegFrame
        if frame is not None:
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
        time.sleep(0.03)


# ---------- Camera Setup ----------
picam2 = Picamera2()
config = picam2.create_video_configuration(
    main={
        "size": (cameraWidth, cameraHeight),
        "format": "RGB888",
    }
)
picam2.configure(config)
picam2.start()
time.sleep(2)
picam2.set_controls(
    {
        "AeEnable": True,
        "AwbEnable": True,
        "ExposureValue": 1.5,
    }
)


# ---------- Smile Detection Setup ----------
faceCascadePath = (
    "/usr/share/opencv4/haarcascades/"
    "haarcascade_frontalface_default.xml"
)
smileCascadePath = (
    "/usr/share/opencv4/haarcascades/haarcascade_smile.xml"
)
faceCascade = cv2.CascadeClassifier(faceCascadePath)
smileCascade = cv2.CascadeClassifier(smileCascadePath)
if faceCascade.empty() or smileCascade.empty():
    print("Error: OpenCV cascade file could not be loaded.")
    exit()


# ---------- Touch Sensor Setup ----------
touchSensor = Button(touchSensorPin, pull_up=True, bounce_time=0.5)


def getTimestamp():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def setEvent(message):
    global latestEvent
    with stateLock:
        latestEvent = message
    print(message)


def startRecording():
    global isRecording, videoWriter, currentVideoPath
    with stateLock:
        if isRecording:
            return
    currentVideoPath = os.path.join(
        videoFolder,
        f"video_{getTimestamp()}.avi",
    )
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    videoWriter = cv2.VideoWriter(
        currentVideoPath,
        fourcc,
        videoFps,
        (cameraWidth, cameraHeight),
    )
    with stateLock:
        isRecording = True
    setEvent(
        f"Recording started: {os.path.basename(currentVideoPath)}"
    )


def stopRecording():
    global isRecording, videoWriter, currentVideoPath, latestVideoPath
    with stateLock:
        if not isRecording:
            return
        isRecording = False
    if videoWriter is not None:
        videoWriter.release()
        videoWriter = None
    with stateLock:
        latestVideoPath = currentVideoPath
    setEvent(
        "Recording stopped and saved: "
        f"{os.path.basename(currentVideoPath)}"
    )
    currentVideoPath = None


def toggleRecording():
    with stateLock:
        recordingNow = isRecording
    if recordingNow:
        stopRecording()
    else:
        startRecording()


if useReleasedTrigger:
    touchSensor.when_released = toggleRecording
else:
    touchSensor.when_pressed = toggleRecording


# ---------- Main Camera Loop ----------
def cameraLoop():
    global latestJpegFrame, latestPhotoPath, lastSmilePhotoTime
    setEvent("Program started. Browser video is running.")
    while True:
        frameRgb = picam2.capture_array()
        frameBgr = cv2.cvtColor(frameRgb, cv2.COLOR_RGB2BGR)
        success, jpegBuffer = cv2.imencode(
            ".jpg",
            frameBgr,
            [cv2.IMWRITE_JPEG_QUALITY, 85],
        )
        if success:
            with frameLock:
                latestJpegFrame = jpegBuffer.tobytes()

        with stateLock:
            recordingNow = isRecording
        if recordingNow and videoWriter is not None:
            videoWriter.write(frameBgr)

        gray = cv2.cvtColor(frameBgr, cv2.COLOR_BGR2GRAY)
        faces = faceCascade.detectMultiScale(
            gray,
            scaleFactor=1.2,
            minNeighbors=5,
            minSize=(80, 80),
        )
        for (x, y, w, h) in faces:
            faceGray = gray[y : y + h, x : x + w]
            smiles = smileCascade.detectMultiScale(
                faceGray,
                scaleFactor=1.8,
                minNeighbors=20,
                minSize=(25, 25),
            )
            if len(smiles) > 0:
                now = time.time()
                if now - lastSmilePhotoTime > smileCooldownSeconds:
                    photoPath = os.path.join(
                        photoFolder,
                        f"smile_{getTimestamp()}.jpg",
                    )
                    cv2.imwrite(photoPath, frameBgr)
                    with stateLock:
                        latestPhotoPath = photoPath
                    setEvent(
                        "Smile detected. Photo saved: "
                        f"{os.path.basename(photoPath)}"
                    )
                    lastSmilePhotoTime = now
                break

        time.sleep(1 / videoFps)


# ---------- Start Program ----------
cameraThread = threading.Thread(target=cameraLoop)
cameraThread.daemon = True
cameraThread.start()

try:
    print("Open this on your Mac: http://192.168.2.2:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)
except KeyboardInterrupt:
    print("Stopping program...")
finally:
    if videoWriter is not None:
        videoWriter.release()
    picam2.stop()
    print("Camera stopped.")

