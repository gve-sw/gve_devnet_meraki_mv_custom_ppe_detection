#!/usr/bin/env python3
""" Copyright (c) 2023 Cisco and/or its affiliates.
This software is licensed to you under the terms of the Cisco Sample
Code License, Version 1.1 (the "License"). You may obtain a copy of the
License at
           https://developer.cisco.com/docs/licenses
All use of the material herein must be in accordance with the terms of
the License. All rights not expressly granted by the License are
reserved. Unless required by applicable law or agreed to separately in
writing, software distributed under the License is distributed on an "AS
IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
or implied.
"""

__author__ = "Mark Orszycki <morszyck@cisco.com>, Trevor Maco <tmaco@cisco.com>"
__copyright__ = "Copyright (c) 2023 Cisco and/or its affiliates."
__license__ = "Cisco Sample Code License, Version 1.1"

import datetime
import json
import os

import cv2
import meraki
import requests
from flask import Flask, render_template, request, Response, session, jsonify, send_from_directory
from rich.console import Console
from dotenv import load_dotenv

# Load Environment Variables
load_dotenv()
MERAKI_API_KEY = os.getenv("MERAKI_API_KEY")

# Global variables
app = Flask(__name__)
app.secret_key = 'super_duper_secret'

# Rich Console Instance
console = Console()

# Meraki Dashboard Instance
dashboard = meraki.DashboardAPI(MERAKI_API_KEY, suppress_logging=True)

# Configure global dictionaries
CAMERAS = {}
ZONE_PPE = {}

# Global PPE State
current_state = None

# Absolute path to parent directory
parent_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Create Snapshots directory
os.makedirs(f'{parent_directory}/snapshots', exist_ok=True)

# Read in JSON Data Files, populate Global dictionaries
with open(f'{parent_directory}/cameras.json', 'r') as cam_fp, open(f'{parent_directory}/ppe_zones.json', 'r') as zone_fp:
    ppe_zones = json.load(zone_fp)
    cameras = json.load(cam_fp)

    # Build ppe zones
    for zone in ppe_zones:
        ZONE_PPE[zone['ppe_zone_name']] = zone['ppe_items']

    # Add Cameras
    for camera in cameras:
        CAMERAS[camera['serial']] = camera['ppe_zone_name']


# Methods
def getSystemTimeAndLocation():
    """
    Returns location and time of accessing device
    :return: Time and location information
    """
    # request user ip
    userIPRequest = requests.get('https://get.geojs.io/v1/ip.json')
    userIP = userIPRequest.json()['ip']

    # request geo information based on ip
    geoRequestURL = 'https://get.geojs.io/v1/ip/geo/' + userIP + '.json'
    geoRequest = requests.get(geoRequestURL)
    geoData = geoRequest.json()

    # create info string
    location = geoData['country']
    timezone = geoData['timezone']
    current_time = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
    timeAndLocation = "System Information: {}, {} (Timezone: {})".format(location, current_time, timezone)

    return timeAndLocation


# Function to capture frames from the RTSP stream
def generate_frames(rtsp_url):
    """
    Yield RTSP frames back to Flask Dashboard (Live RTSP Feed)
    :param rtsp_url: RTSP URL for Camera
    :return: Individual video frames (fast enough to stitch together live video)
    """
    # Capture video from VideoCapture in cv2
    cap = cv2.VideoCapture(rtsp_url)
    while True:
        # Obtain current frame
        ret, frame = cap.read()
        if not ret:
            break
        # Get frame in bytes
        ret, buffer = cv2.imencode('.jpg', frame)
        frame = buffer.tobytes()

        # Return frame in bytes to html, streamed to flask page
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')


def find_image_by_serial(serial_number):
    """
    Find annotated snapshot using serial (included in file name) - annotated file = bounding boxes included
    :param serial_number: Camera Serial
    :return: File name
    """
    # List of image filenames
    image_filenames = os.listdir(f'{parent_directory}/snapshots')

    # Iterate through images list, find most recent annotated image
    for filename in image_filenames:
        if serial_number in filename and 'annotated' in filename:
            return filename

    return None


# Routes
@app.route('/')
def index():
    """
    Main route, display camera selection panel
    """
    error_code = None

    # Render page
    return render_template('index.html', hiddenLinks=False, timeAndLocation=getSystemTimeAndLocation(),
                           errorcode=error_code, camera_list=CAMERAS, display_feed=False)


@app.route('/display', methods=["POST"])
def display():
    """
    Display page, show RTSP live stream in left rail, most recent annotated image in right rail
    """
    error_code = None

    # Get Serial from form
    serial_number = request.form['camera_selected']
    console.print(f"Display RTSP and PPE Detection Stream from camera serial: [blue]{serial_number}[/]")

    # Determine correct ppe for zone associated to camera
    required_ppe = None
    ppe_zone_name = None
    if serial_number in CAMERAS:
        ppe_zone_name = CAMERAS[serial_number]

        if ppe_zone_name in ZONE_PPE:
            required_ppe = ZONE_PPE[ppe_zone_name]

    # Enable RTSP/Get RTSP stream
    response = dashboard.camera.updateDeviceCameraVideoSettings(serial_number, externalRtspEnabled=True)

    rtsp_url = response['rtspUrl']
    console.print(f"RTSP stream link obtained: [blue]{rtsp_url}[/]")

    # Store stream in session object for requests to /video_feed
    session['rtsp_url'] = rtsp_url

    # Render page
    return render_template('index.html', hiddenLinks=False, timeAndLocation=getSystemTimeAndLocation(),
                           errorcode=error_code, camera_list=CAMERAS, ppe_zone_name=ppe_zone_name,
                           required_ppe=required_ppe, current_state=current_state,
                           display_feeds=True, serial_number=serial_number)


@app.route('/update_state', methods=['POST'])
def update_state():
    """
    Update PPE State to display on Webpage (updated with a new state after analyzing a snapshot image)
    """
    global current_state

    # New request received from PPE code
    current_state = request.json.get('ppe_state')
    console.print(f"New PPE State Detected and Updated: [blue]{current_state}[/]")

    return "State updated successfully"


@app.route('/get_state')
def get_state():
    """
    Get current PPE state (periodically called via javascript to update the front-end)
    """
    return jsonify({'current_state': current_state})


@app.route('/retrieve_image/<serialNumber>')
def retrieve_image(serialNumber):
    """
    Get the most recent annotated image for display (based on Serial section from front end index.html page)
    :param serialNumber: Camera Serial to grab the most recent annotated image for
    """
    # Get the most recent annotated image (if it exists)
    image_filename = find_image_by_serial(serialNumber)

    # Use send_from_directory to send the image file to the client
    return send_from_directory(f'{parent_directory}/snapshots', image_filename)


@app.route('/video_feed')
def video_feed():
    """
    Enable RTSP stream for local camera display on Webpage. Return frames yielded from method, display RTSP stream on
    web poge
    """
    rtsp_url = session.get('rtsp_url')  # Retrieve the RTSP URL from the session
    if rtsp_url:
        return Response(generate_frames(rtsp_url), mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        return "No RTSP URL available"


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=4000, debug=False)
