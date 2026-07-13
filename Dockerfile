FROM python:3.12-slim

WORKDIR /app

COPY teleadmin_project/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

WORKDIR /app/teleadmin_project

CMD ["python", "bot.py"]
