FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orcavault_mcp.py .
ENV HOST=0.0.0.0
ENV MCP_TRANSPORT=sse
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "python orcavault_mcp.py"]
