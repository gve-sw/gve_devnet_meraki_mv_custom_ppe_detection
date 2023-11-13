FROM python:3.11

WORKDIR /ppe_app/visualization_dashboard

RUN apt-get update && apt-get install ffmpeg libsm6 libxext6  -y

COPY ./requirements.txt /ppe_app
RUN pip install -r /ppe_app/requirements.txt

COPY ./ppe_app/cameras.json /ppe_app
COPY ./ppe_app/ppe_zones.json /ppe_app

COPY ./ppe_app/visualization_dashboard /ppe_app/visualization_dashboard
CMD ["python", "./app.py"]