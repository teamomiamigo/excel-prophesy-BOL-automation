import json
import os
import socket

import boto3
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


# mapping hostnames becayuse of a DNS resolution broken right now. 
# confirmed with testing

# follow up: find out why the link-local resolver isn't reachable in this VPC
# right now workaround for unresolved platform question, not a permanent fix

_STATIC_DNS_OVERRIDES = {
    "secretsmanager.us-east-1.amazonaws.com": "172.31.20.236",
    "sg360-bol-aurora.cluster-cppw8xnzpofk.us-east-1.rds.amazonaws.com": "172.31.11.70",
    "AWP-SQL-PROD": "172.17.23.172",
    "AWP-SQL-PROD.ad.sg360.com": "172.17.23.172",
}

_original_getaddrinfo = socket.getaddrinfo

# sufix match for exact-map entry. Any S3 front-end IP serves any bucket (routing is by Host header) and VPC's
# S3 gateway endpoints route entire S3 public prefix list, so pinning only one currenlty-valid IP works.
# If S3 ever retires specific IP, fast connection timeout will happen but will be fixed by re-resolving s3.us-east-1.amazonaws.com from any office machine and updating here.

_S3_STATIC_IP = "52.216.36.234"
_S3_HOST_SUFFIX = ".s3.us-east-1.amazonaws.com"


def _patched_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, str):
        if host in _STATIC_DNS_OVERRIDES:
            host = _STATIC_DNS_OVERRIDES[host]
        elif host == "s3.us-east-1.amazonaws.com" or host.endswith(_S3_HOST_SUFFIX):
            host = _S3_STATIC_IP
    return _original_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    USE_MOCK_DATA: bool = True
    MOCK_INVOICES: bool = True   # skips alg invoices during pull

    EIA_API_KEY: str = ""        # EIA api when it was being used (and needs to be used later)

    DATABASE_URL: str = "postgresql://sg360_user:localpass@localhost:5432/sg360_bol"

    # SQL Server credentials for AWP-SQL-PROD
    SQLSERVER_USER: str = ""
    SQLSERVER_PASSWORD: str = ""

    # ODBC driver name pyodbc connects with 
    SQLSERVER_ODBC_DRIVER: str = "ODBC Driver 18 for SQL Server"

    # SG360 tech prd 1 but currently isn't being used but will be (according to AI research as of  7-20)
    TECH_PRD1_SERVER: str = "SG360-TECH-PRD1"
    TECH_PRD1_USER: str = ""
    TECH_PRD1_PASSWORD: str = ""

    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    SMTP_HOST: str = "smtp.office365.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = "logistics@sg360.com"
    EMAIL_TO_MARY: list[str] = ["mary@sg360.com"]
    EMAIL_TO_KATIE: list[str] = ["katie@sg360.com"]
    EMAIL_SUBJECT_PREFIX: str = "BOL Approvals —"

    # IMAP polling — same O365 account as SMTP
    IMAP_HOST: str = "outlook.office365.com"
    IMAP_PORT: int = 993
    IMAP_MAILBOX: str = "INBOX"
    ALG_SENDER_EMAIL: str = ""  # filter by sender email

    INVOICE_FOLDER: str = ""  # Absolute path to watch folder; empty = disabled
    INVOICE_S3_BUCKET: str = ""  # S3 bucket for uploaded invoice PDFs; empty = disabled (mock mode, or not yet provisioned)

    # AI agent layer (backend/agents/) — direct Anthropic API, not Bedrock.
    # Empty key = classify.py's deterministic template fallback only, no API call
    # attempted (see backend/agents/llm.py) — safe default for a machine with no key set.
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-haiku-4-5"
    # Base URL the one-click "approve all recommended" email link is built from
    # (settings.APP_BASE_URL + "/api/agents/email-batch-approve?..."). Must be a
    # reachable URL for whoever clicks the email — localhost for the demo, the
    # real deployed URL once this runs against production.
    APP_BASE_URL: str = "http://localhost:8000"

    APP_NAME: str = "SG360 BOL Reconciliation"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True


def _load_aws_secrets(secret_name: str) -> dict:
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def _build_database_url_from_rds_secret(secret_arn: str, host: str, port: int, database: str) -> str:
    # RDS-managed secrets are region-specific, so the secret ARN must be in the same
    # region as lambda function.
    creds = _load_aws_secrets(secret_arn)
    url = URL.create(
        drivername="postgresql",
        username=creds["username"],
        password=creds["password"],
        host=host,
        port=port,
        database=database,
    )
    # url.render_as_string()/str(url) default to hide_password=True 
    # (masks the password as "***") — correct for logging, wrong here, since this string
    # is consumed directly by create_engine(). Must pass False explicitly.
    return url.render_as_string(hide_password=False)


_aws_secret_name = os.environ.get("AWS_SECRET_NAME")
settings = Settings(**_load_aws_secrets(_aws_secret_name)) if _aws_secret_name else Settings()

# DB credentials always come from AWS's own auto-rotated RDS-managed secret 
_rds_master_secret_arn = os.environ.get("RDS_MASTER_SECRET_ARN")
if _rds_master_secret_arn:
    settings.DATABASE_URL = _build_database_url_from_rds_secret(
        _rds_master_secret_arn,
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ["DB_NAME"],
    )
