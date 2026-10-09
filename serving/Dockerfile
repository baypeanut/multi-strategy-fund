FROM python:3.12.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-serving.txt ./
RUN pip install --no-cache-dir -r requirements-serving.txt \
    && addgroup --system --gid 10001 serving \
    && adduser --system --uid 10001 --ingroup serving serving
COPY core/__init__.py ./core/__init__.py
COPY core/llm/ ./core/llm/
COPY serving/ ./serving/
USER 10001:10001
EXPOSE 8081
CMD ["uvicorn", "serving.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--no-access-log"]
