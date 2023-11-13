FROM python:3.11

WORKDIR /microsoft_teams_app

RUN pip install Flask rich python-dotenv

COPY . .

CMD ["python", "./serve_images.py"]