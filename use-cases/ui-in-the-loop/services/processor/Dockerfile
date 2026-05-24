FROM python:3.11-slim

WORKDIR /app

COPY ui-in-the-loop/services/processor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY ui-in-the-loop/services/processor/service.py .

EXPOSE 8080

CMD ["python", "service.py"]
