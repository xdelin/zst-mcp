FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
# Remove Windows-only packages and add missing ones
RUN sed -i '/pywin32/d' requirements.txt && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir fastmcp uvicorn

# Copy application code
COPY . .

# Copy run script from parent (will be handled by packaging script to place it here)
# For now, assume run_mcp_server.py is copied to root of mcp_server context
# But wait, run_mcp_server.py is in root of repo.
# I will instruct the user or the packaging script to copy it.

# Expose port
EXPOSE 5100

# Run command
# We will use a simple python script to run it, similar to run_mcp_server.py
# Or just run uvicorn directly if we can import the app.
# run_mcp_server.py does: from mcp_server.server import mcp
# If we are IN mcp_server directory, it is `from server import mcp`
CMD ["uvicorn", "server:mcp.sse_app", "--host", "0.0.0.0", "--port", "5100", "--factory"]
