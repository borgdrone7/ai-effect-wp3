FROM python:3.11-slim
WORKDIR /app
COPY ui-in-the-loop/controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ ./common/
COPY ui-in-the-loop/controller/app.py .
COPY ui-in-the-loop/controller/index.html .
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
