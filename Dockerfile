FROM public.ecr.aws/lambda/python:3.13

# Microsoft ODBC driver for SQL Server — required by pyodbc for live-mode
# connections to AWP-SQL-PROD / SG360-TECH-PRD1. Base image is Amazon Linux,
# so Microsoft's RHEL/CentOS yum repo is the correct source.
RUN curl -o /etc/yum.repos.d/mssql-release.repo https://packages.microsoft.com/config/rhel/9/prod.repo \
    && ACCEPT_EULA=Y dnf install -y msodbcsql18 unixODBC-devel \
    && dnf clean all

# Install backend dependencies first (better layer caching)
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the backend application code needed at runtime.
# backend/test_data/ is intentionally included — it is read by main.py in mock mode.
COPY backend ./backend

# Lambda container images require the handler as MODULE.HANDLER_ATTR
CMD ["backend.main.handler"]
