# Приложение «Граф денег»: пайплайн + карта + ассистент в одном контейнере. База — отдельный сервис в compose.yaml.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt requirements-docker.txt ./
RUN pip install -r requirements.txt -r requirements-docker.txt
COPY . .
EXPOSE 8000
CMD ["python", "serve.py"]
