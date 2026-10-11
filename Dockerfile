FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "from transformers import AutoTokenizer, VitsModel; name = 'facebook/mms-tts-ben'; AutoTokenizer.from_pretrained(name); VitsModel.from_pretrained(name)"

COPY . .

CMD ["python", "bot.py"]