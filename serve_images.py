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

import threading
import os
import time
from flask import Flask, send_from_directory
from rich.console import Console

import config

# Global variables
app = Flask(__name__)

# Rich Console Instance
console = Console()


def cleanup_old_images(images_directory, retention_period_days):
    """
    Remove images after they exceed the retention period (this controls hosted_images folder size) - run in background thread
    :param images_directory: hosted_images directory
    :param retention_period_days: Number of Days to retain image
    """
    current_time = time.time()
    retention_period_seconds = retention_period_days * 24 * 60 * 60  # Convert days to seconds

    for filename in os.listdir(images_directory):
        # Find file name, determine time the file was created
        file_path = os.path.join(images_directory, filename)
        creation_time = os.path.getctime(file_path)

        # If the current time exceeds creation time plus retention period, remove the file (removes hosted file from
        # Microsoft Teams space)
        if current_time - creation_time > retention_period_seconds:
            os.remove(file_path)
            console.print(f"Removed {file_path}")


def cleanup_thread(images_directory, retention_period_days):
    """
    Spawn thread to clean up hosted_images files after they exceed the retention period
    :param images_directory: hosted_images directory
    :param retention_period_days: Number of Days to retain image
    """
    while True:
        # cleanup old hosted images ( > retention period)
        console.print(f"Running clean_up_hosted_images procedure.")

        cleanup_old_images(images_directory, retention_period_days)
        time.sleep(12 * 60 * 60)  # Sleep for 12 hours (adjust as needed)


@app.route('/serve_image/<filename>')
def serve_image(filename):
    """
    Serve image publicly for Microsoft Teams messages
    :param filename: Target filename to serve
    :return: File in bytes
    """
    return send_from_directory('static/hosted_images', filename)


if __name__ == "__main__":
    # Start background thread to clean up hosted images
    cleanup_thread = threading.Thread(target=cleanup_thread, args=('static/hosted_images', config.IMAGE_RETENTION_DAYS))
    cleanup_thread.daemon = True
    cleanup_thread.start()

    app.run(host='0.0.0.0', port=3500, debug=False)
