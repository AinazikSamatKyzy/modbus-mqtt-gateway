FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ common/
COPY sensor_sim/ sensor_sim/
COPY gateway/ gateway/
COPY monitor.py .

RUN useradd --create-home appuser
USER appuser
ENV PYTHONUNBUFFERED=1

# The same image runs either service; docker-compose picks the command
CMD ["python", "-m", "gateway.gateway"]
