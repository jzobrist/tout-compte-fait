FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
EXPOSE 8000
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8000", "app:create_app()"]
