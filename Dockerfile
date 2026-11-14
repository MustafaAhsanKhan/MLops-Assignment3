# Prediction API image. The model is baked in (serving/model/, produced by serving/export_model.py),
# so the image tag model-v<N> identifies exactly which registry version is deployed.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY serving/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY serving/app.py .
COPY serving/model/ model/
RUN useradd --system --uid 10001 app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
