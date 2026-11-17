FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD ["sh", "-c", "python -m streamlit run directory_app.py --server.address 0.0.0.0 --server.port ${PORT:-10000}"]
