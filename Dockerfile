FROM python:3.9-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application file (using quotes for space in filename)
COPY ["linkchecker PUBLIC.py", "./linkchecker PUBLIC.py"]

# Start the worker
CMD ["python", "linkchecker PUBLIC.py"] 