FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app files
COPY . .

# Ensure upload directories exist with correct permissions
RUN mkdir -p videos/temp && chmod -R 777 videos

# Expose the default Hugging Face port
EXPOSE 7860

# Run with gunicorn (bind to 0.0.0.0 so it's accessible inside the container)
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "app:app", "--timeout", "120"]
