#!#!/usr/bin/env python3
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

import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime

import cv2
import meraki
import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from ultralytics import YOLO

import config

# Load Environment Variables
load_dotenv()
MERAKI_API_KEY = os.getenv("MERAKI_API_KEY")
MICROSOFT_TEAMS_URL = os.getenv("MICROSOFT_TEAMS_URL")
IMAGE_RETENTION_DAYS = os.getenv("IMAGE_RETENTION_DAYS")

# Rich Console Instance
console = Console()

# Meraki Dashboard Instance
dashboard = meraki.DashboardAPI(MERAKI_API_KEY, suppress_logging=True)

# Configure global dictionaries
CAMERAS = {}
ZONE_PPE = {}

# Absolute path to parent directory
current_directory = os.path.dirname(os.path.abspath(__file__))
parent_directory = os.path.dirname(current_directory)

# YOLOv8 ML Model File
MODEL = YOLO(f"{current_directory}/ppe_dataset/weights/best.pt")

# Minimum confidence threshold for detections
CONFIDENCE = 0.5

# Define a dictionary to keep track of active threads
active_threads = {}

# Read in JSON Data Files, populate Global dictionaries
with open(f'{parent_directory}/cameras.json', 'r') as cam_fp, open(f'{parent_directory}/ppe_zones.json', 'r') as zone_fp:
    ppe_zones = json.load(zone_fp)
    cameras = json.load(cam_fp)

    # Build ppe zones
    for zone in ppe_zones:
        zone_name = zone['ppe_zone_name']
        del zone['ppe_zone_name']
        ZONE_PPE[zone_name] = zone['ppe_items']

    # Add Cameras
    for camera in cameras:
        serial = camera['serial']
        del camera['serial']
        CAMERAS[serial] = camera

# Create Snapshots directory
os.makedirs(f'{parent_directory}/snapshots', exist_ok=True)

def generate_snapshot(serial):
    """
    Take snapshot of MV camera's entire frame at the current time
    :param serial: MV Camera Serial
    :return: URL link to MV Snapshot
    """
    # Generate snapshot of current full frame
    response = dashboard.camera.generateDeviceCameraSnapshot(serial)

    if 'url' in response:
        console.print(f"Obtained MV Snapshot: {response['url']}")
        return response['url']
    else:
        return None


def download_file(file_name, file_url, folder):
    """
    Download file (MV snapshot, DeepArts photo) from URL to local folder
    :param file_name: new file name for downloaded file
    :param file_url: file url
    :param folder: destination folder for new file
    :return: new file path for downloaded file
    """
    attempts = 1
    while attempts <= 50:
        r = requests.get(file_url, stream=True)
        if r.ok:
            console.print(f'- Retried {attempts} times until successfully retrieved {file_url}')

            # Check if the file type is valid (JPEG, PNG for Webex), Meraki snapshot is always JPEG
            content_type = r.headers['Content-Type'].split('/')

            temp_file = f'{folder}/{file_name}.{content_type[1]}'

            # Open temp file and write byte content into file
            with open(temp_file, 'wb') as f:
                for chunk in r:
                    f.write(chunk)

            console.print(f'- [green]Successfully downloaded file: {temp_file}[/]')
            return temp_file

        else:
            attempts += 1
    print(f'- Unsuccessful in 50 attempts retrieving {file_url}')
    return None

def send_annotated_image_to_hosted_app(image_file_path, annotated_hosted_name):
    """
    Once an image is annotated, send the annotated image to the hosting app for Microsoft Teams messages
    :param image_file_path: Annotated image file path
    :param annotated_hosted_name: Annotated image name with unique ID (for hosting unique images)
    :return:
    """
    # Open image in binary
    files = {'image': (annotated_hosted_name, open(image_file_path, 'rb'))}

    # Send image to hosting app
    response = requests.post(config.HOSTING_APP_URL + '/receive_image', files=files)

    if response.status_code == 200:
        print("Image sent successfully to the other app!")
    else:
        print("Failed to send the image to the other app.")

def create_label(img, color, class_name, confidence, top_left):
    """
    Create label for bounding box in annotated image
    :param img: Source Image with bounding boxes
    :param color: Label color
    :param class_name: Label name
    :param confidence: Label confidence percentage (for display)
    :param top_left: Top left corner of bounding box
    :return: Image with labels attached to the top left corner of bounding boxes
    """
    # For the text background
    text = class_name + " " + str(round(confidence * 100, 2)) + "%"

    # Finds space required by the text so that we can put a background with that amount of width.
    (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1, 2)

    # Prints the text
    img = cv2.rectangle(img, (top_left[0], top_left[1] - 40), (top_left[0] + w, top_left[1]), color, -1)
    img = cv2.putText(img, text, (top_left[0], top_left[1] - 5),
                      cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
    return img


def detect_ppe_on_image(serial_number, snapshot_path, required_ppe):
    """
    Run YOLOv8 prediction on image (MV snapshot)
    :param serial_number: MV serial number (image path name)
    :param snapshot_path: Path to raw MV snapshot
    :param required_ppe: Required PPE dictionary
    :return: Detected classes (to determine ppe violation)
    """
    # Run prediction on image with YOLO model
    results = MODEL.predict(snapshot_path, conf=CONFIDENCE)
    result = results[0]

    # Open image to apply boxes
    img = cv2.imread(snapshot_path)

    # Extract the bounding box coordinates and dimensions (normalized by image size), extract the class name,
    # extract detection prob
    outputs = []
    classes = []
    for box in result.boxes:
        # class id (box id), translated class name
        class_id = box.cls[0].item()
        class_name = result.names[class_id]

        # Ignore 'Person Class' to clean up image, ignore PPE items we aren't enforcing in this zone (both no and
        # normal versions of class)
        ppe_comparison = class_name.replace('No', '').strip()
        if class_name == 'Person' or (ppe_comparison in required_ppe and required_ppe[ppe_comparison] == False):
            continue

        # class prob(box probability)
        prob = round(box.conf[0].item(), 2)

        # Centroid X,Y and width/height of bounding box normalized to image size
        x1, y1, nw, nh = [
            round(x) for x in box.xywh[0].tolist()
        ]

        outputs.append([
            x1, y1, nw, nh, class_name, prob
        ])

        classes.append(class_name)

    # Create boxes and labels for valid classes on this camera
    for output in outputs:
        # Calculate top_left and bottom_right of bounding box for drawing boxes
        top_left = (int(output[0] - output[2] / 2), int(output[1] - output[3] / 2))
        bottom_right = (int(output[0] + output[2] / 2), int(output[1] + output[3] / 2))

        # Create Box (green for PPE item, red for 'No' ppe classes), add label (name, percentage detection)
        if 'No' in output[4]:
            color = (0, 0, 255)
            cv2.rectangle(img, top_left, bottom_right, color, 3)

            # Attach class label and confidence label
            img = create_label(img, color, output[4], output[5], top_left)
        else:
            color = (0, 255, 0)
            cv2.rectangle(img, top_left, bottom_right, color, 3)

            # Attach class label and confidence label
            img = create_label(img, color, output[4], output[5], top_left)

    # Save image output to snapshots folder
    cv2.imwrite(f'{parent_directory}/snapshots/{serial_number}_snapshot_annotated.jpeg', img)

    return classes


def detect_ppe_state(ppe_detected, required_ppe):
    """
    Determine if all PPE is present in desired zone or not (adjust 'state' - Valid, Invalid, Unknown appropriately)
    :param ppe_detected: PPE classes detected in image
    :param required_ppe: Required PPE dictionary to check
    :return: Boolean representing if all PPE is present
    """
    # Extract the object names with value 1 from the dictionary
    ppe_to_check = [key for key, value in required_ppe.items() if value == True]

    # Check if a PPE Violation is detected - No Class (Case 1):
    no_class_present = any('No' in item for item in ppe_detected)
    if no_class_present:
        return False

    # Check if all PPE is present (Case 2)
    if len(ppe_detected) > 0:
        all_ppe_present = all(item in ppe_to_check for item in ppe_detected)

        # Determine if proper PPE worn (based on zone)
        if all_ppe_present:
            return True

    # Unable to determine if all PPE detected (ex: not all objects could be reasonably detected, no objects present)
    return None


def process_message(serial_number, payload_dict):
    """
    Start processing received detection of a person from MQTT server, generate snapshot and run detection model -
    main driver - executed via thread to not block main MQTT thread
    :param serial_number: MV Serial number where person is detected
    :param payload_dict: MQTT message payload
    """
    # Determine correct ppe for zone associated to camera
    if serial_number in CAMERAS:
        ppe_zone_name = CAMERAS[serial_number]["ppe_zone_name"]

        if ppe_zone_name in ZONE_PPE:
            required_ppe = ZONE_PPE[ppe_zone_name]

            console.print(Panel.fit("Running Image Prediction:", title='Step 1'))
            console.print(f"[blue]Camera:[/] {serial_number}, [blue]PPE Zone:[/] {ppe_zone_name}")

            # Generate and download snapshot
            image_url = generate_snapshot(serial_number)
            snapshot_path = download_file(f'{serial_number}_snapshot', image_url, f'{parent_directory}/snapshots')

            # Run Inference logic here (detect ppe! - where the magic happens!)
            ppe_detected = detect_ppe_on_image(serial_number, snapshot_path, required_ppe)
            ppe_state = detect_ppe_state(ppe_detected, required_ppe)

            console.print(Panel.fit("PPE Verdict (Microsoft Teams Message)", title='Step 2'))

            if ppe_state is False:
                console.print('[red]PPE Violation detected! One or more zone items is missing...[/]')

                # Copy annotated result image to hosted_images folder with unique id (to guarantee uniqueness)
                unique_id = str(uuid.uuid4())[:8]

                image_name = snapshot_path.split('/')[-1].split('.jpeg')[0]
                annotated_name = image_name + '_annotated.jpeg'
                annotated_hosted_name = image_name + f'_hosted_{unique_id}.jpeg'

                # Try to send image to hosting app
                try:
                    send_annotated_image_to_hosted_app(f'{parent_directory}/snapshots/{annotated_name}', annotated_hosted_name)
                except Exception as e:
                    console.print(f'Unable to send image to hosting app: {str(e)}')

                # On violation, send Microsoft Teams message
                console.print('Sending Microsoft teams message...')
                send_microsoft_teams_message(serial_number, annotated_hosted_name)
            elif ppe_state is True:
                console.print('[green]All PPE is present for this zone![/]')
            else:
                console.print('Unable to determine if full PPE is present...')

            # Send State Update to API Endpoint on Flask App
            try:
                flask_app_url = f"{config.VISUALIZATION_APP_URL}/update_state"
                state_data = {"ppe_state": ppe_state}

                console.print(f"Updating PPE State to [blue]{ppe_state}[/]...")

                response = requests.post(flask_app_url, json=state_data)

                if response.status_code == 200:
                    console.print("- [green]State successfully sent to flask app[/]")
                else:
                    console.print(f"- [red]Failed to update state, status code: {response.status_code}[/]")

            except Exception as e:
                console.print(f"- [red]Failed to update state, error: {str(e)}[/]")

            # Sleep to prevent spam processing
            sleep_time = 20

            console.print(f'Waiting for {sleep_time} seconds...')
            time.sleep(sleep_time)

            # Remove the thread entry from the active_threads dictionary when done
            del active_threads[serial_number]
        else:
            console.print('[red]PPE Zone name not defined, skipping detection...[/]')
    else:
        console.print('[red]No PPE Zone Defined for Camera, skipping detection...[/]')


def send_microsoft_teams_message(serial_number, annotated_hosted_name):
    """
    Send Microsoft Teams message when a PPE violation has been detected (include image, use default.json card)
    :param serial_number: MV camera serial where violation was detected
    :param annotated_hosted_name: Name of image file hosted locally in flask app for display in adaptive card
    """
    camera_data = CAMERAS[serial_number]

    # Load card file
    with open(f'{current_directory}/cards/default_card.json', "r") as json_file:
        card_payload = json.load(json_file)

    # Get data and time stamp
    current_datetime = datetime.now()
    formatted_datetime = current_datetime.strftime("%B %d, %Y %H:%M")

    # Plug in values into payload
    card_payload['body'][1]['text'] = formatted_datetime
    card_payload['body'][2]['columns'][0]['items'][0]['facts'][0]['value'] = serial_number
    card_payload['body'][2]['columns'][0]['items'][0]['facts'][1]['value'] = camera_data['camera_location']
    card_payload['body'][2]['columns'][1]['items'][0]['facts'][0]['value'] = camera_data['ppe_zone_name']

    # Determine Image Path
    card_payload['body'][3]['url'] = f"{config.SERVE_IMAGES_URL}/serve_image/{annotated_hosted_name}"

    # Set Retention Period Warning
    card_payload['body'][4][
        'text'] = f"Note: Image will automatically be removed in {IMAGE_RETENTION_DAYS} day(s)"

    headers = {'Content-Type': 'application/json'}
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card_payload
            }
        ]
    }

    # Send the image to the Teams channel
    response = requests.post(MICROSOFT_TEAMS_URL, headers=headers, json=payload)
    console.print("- [green]Successfully sent Microsoft Teams notification[/]")


def on_connect(client, userdata, flags, rc):
    """
    Subscribe MQTT Client on successful connection
    :param client: MQTT Local Client
    :param rc: MQTT Connection Code
    """
    console.print("Connected with code: " + str(rc))
    for camera in CAMERAS:
        if CAMERAS[camera]['camera_zone_id'] != '':
            camera_zone = CAMERAS[camera]['camera_zone_id']
        else:
            camera_zone = '0'

        client.subscribe("/merakimv/" + camera + '/' + camera_zone)


def on_message(client, userdata, msg):
    """
    Callback when MQTT message received. Check if a person has been detected. If detected, run PPE detection algorithm
    :param msg: MQTT Message from MV
    """
    payload = msg.payload.decode("utf-8")
    payload_dict = json.loads(payload)

    # Extract the camera SN from the mqtt topic header and use it to generate MV snapshot. Generate
    # the time of snapshot by converting the epoc time in the mqtt message
    # create a payload of url, mv name, time of trigger and serial number:
    serial_number = msg.topic.split("/")[2]

    # Check if a thread is already active for the serial number
    if serial_number not in active_threads:
        console.print(f"Alert from camera {serial_number}: {payload_dict}")

        if 'counts' in payload_dict and payload_dict['counts']['person'] > 0:
            console.print("[green]People detected on camera![/] Starting detection thread...")

            # Create a new thread and store it in the active_threads dictionary
            message_thread = threading.Thread(target=process_message, args=(serial_number, payload_dict))
            active_threads[serial_number] = message_thread
            message_thread.start()


if __name__ == "__main__":
    try:
        client = mqtt.Client()
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(config.MQTT_SERVER, config.MQTT_PORT, 60)
        client.loop_forever()

    except Exception as ex:
        console.print("[red]MQTT failed to connect or receive msg from mqtt, due to: \n {0}[/]".format(ex))
