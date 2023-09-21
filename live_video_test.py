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

import meraki

from ultralytics import YOLO
import cv2
import supervision as sv

import config

# Meraki Dashboard Instance
dashboard = meraki.DashboardAPI(config.MERAKI_API_KEY, suppress_logging=True)

# Yolov8 ML Model File
MODEL = YOLO("ppe_dataset/weights/best.pt")

# Minimum confidence threshold for detections
CONFIDENCE = 0.5

CAMERA = {
    "serial": "Q2EV-M982-WW24",
    "ppe_zone_name": "_all_",
    "camera_zone_id": "0",
    "camera_location": "1P"
}

PPE_ZONE = {
    "helmet": True,
    "vest": True,
    "glasses": True,
    "black_shoes": True
}


def get_org_id(org_name):
    # Get Meraki Orgs
    orgs = dashboard.organizations.getOrganizations()

    org_id = None
    # Get Meraki Org Ids for each Meraki Org Name specified
    for org in orgs:
        if org['name'] == org_name:
            org_id = org['id']

    return org_id


def enable_rtsp():
    # Enable rtsp for camera serial, return rtsp link
    response = dashboard.camera.updateDeviceCameraVideoSettings(CAMERA['serial'], externalRtspEnabled=True)
    return response['rtspUrl']


def start_prediction_loop(model, conf_threshold, rtsp_stream, zone_ppe):
    # Define box for detections (red boxes for no item positions, green boxes for item present positions based on
    # index in data.yaml)
    box_annotator = sv.BoxAnnotator(
        thickness=2,
        text_thickness=1,
        text_scale=0.5,
        color=sv.ColorPalette.from_hex(['#ff0000', '#ff0000', '#ff0000', '#00ff00', '#00ff00', '#00ff00', '#00ff00'])
    )

    # Process each result (inference on an individual video frame)
    for result in model.predict(source=rtsp_stream, stream=True, conf=conf_threshold):
        # Capture original frame to modify with supervision
        frame = result.orig_img

        # Annotate image with new bounding boxes
        detections = sv.Detections.from_yolov8(result)

        # Store Unique Object id in detections object (check if no object in scene - prevent crash)
        if result.boxes.id is not None:
            detections.tracker_id = result.boxes.id.cpu().numpy().astype(int)

        # Ignore Person class in customer dataset
        detections = detections[detections.class_id != 3]

        # Override default bounding box labels
        labels = [
            f"#{class_id} {model.model.names[class_id]} {confidence:0.2f}"
            for area, box_area, confidence, class_id, tracker_id
            in detections
        ]

        frame = box_annotator.annotate(scene=frame, detections=detections, labels=labels)

        if labels:
            print('Person Detected')

        # Extract the object names with value 1 from the dictionary
        ppe_to_check = [key for key, value in zone_ppe.items() if value == 1]

        # Check if all objects with value 1 are present in the list
        all_ppe_present = all(
            any(f" {object_name} " in item for object_name in ppe_to_check) for item in labels)

        # Determine if proper PPE worn (based on zone)
        if all_ppe_present:
            print("All required PPE is present.")
        else:
            print("Not all required objects are present.")

        cv2.imshow('yolov8', frame)

        # Break the loop if 'esc' is pressed
        if cv2.waitKey(30) == 27:
            break


if __name__ == "__main__":
    # Get Org id
    org_id = get_org_id(config.MERAKI_ORG_NAME)

    # Enable RTSP for each camera, obtain RTSP links
    rtsp_link = enable_rtsp()
    start_prediction_loop(MODEL, CONFIDENCE, rtsp_link, PPE_ZONE)
