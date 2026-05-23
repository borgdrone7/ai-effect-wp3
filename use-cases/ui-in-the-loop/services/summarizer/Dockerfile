FROM python:3.11-slim

WORKDIR /app

COPY ui-in-the-loop/services/summarizer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY ui-in-the-loop/services/summarizer/service.py .

EXPOSE 8080

CMD ["python", "service.py"]
