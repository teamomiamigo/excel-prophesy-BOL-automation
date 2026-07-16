import json
import os
import socket

import boto3
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

# DNS resolution is broken for this Lambda's VPC/subnet combination — the
# link-local resolver Lambda normally uses (169.254.x.x) is unreachable here,
# confirmed via direct diagnostic testing (raw UDP query to it times out,
# while direct TCP connections to every real destination below succeed
# immediately). That resolver path isn't something a security group can fix
# (similar to the EC2 metadata service, it isn't filtered by normal SG
# rules), so instead of routing around it with more network changes, this
# statically maps the handful of hostnames this app actually needs straight
# to their known private IPs, bypassing DNS entirely for just those lookups.
# Everything else (TLS certificates, connection behavior) is untouched since
# only the address-lookup step is intercepted — the correct hostname is still
# sent for TLS SNI/certificate validation.
#
# Follow-up worth doing later: find out from Joseph/AWS team *why* the
# link-local resolver isn't reachable in this VPC — this is a workaround for
# an unresolved platform question, not a permanent fix.
_STATIC_DNS_OVERRIDES = {
    "secretsmanager.us-east-1.amazonaws.com": "172.31.20.236",
    "sg360-bol-aurora.cluster-cppw8xnzpofk.us-east-1.rds.amazonaws.com": "172.31.11.70",
    "AWP-SQL-PROD": "172.17.23.172",
    "AWP-SQL-PROD.ad.sg360.com": "172.17.23.172",
    "SG360-TECH-PRD1": "172.17.23.5",
    "SG360-TECH-PRD1.ad.sg360.com": "172.17.23.5",
}

_original_getaddrinfo = socket.getaddrinfo

# S3 hostnames are bucket-prefixed (sg360-bol-invoices.s3.us-east-1.amazonaws.com),
# so a suffix match is needed rather than another exact-map entry. Any S3
# front-end IP serves any bucket (routing is by Host header), and the VPC's S3
# Gateway endpoints route the entire S3 public prefix list, so pinning one
# currently-valid IP works. If S3 ever retires this specific IP the symptom is
# a fast connect timeout (the boto3 clients use short-timeout Config), fixed by
# re-resolving s3.us-east-1.amazonaws.com from any office machine and updating here.
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
    MOCK_INVOICES: bool = True   # skip get_alg_invoice() during pull; leave invoice fields null

    EIA_API_KEY: str = ""        # eia.gov/developer — free; needed for live FSC diesel price lookup

    DATABASE_URL: str = "postgresql://sg360_user:localpass@localhost:5432/sg360_bol"

    # SQL Server credentials for AWP-SQL-PROD (leave blank to use Windows auth)
    SQLSERVER_USER: str = ""
    SQLSERVER_PASSWORD: str = ""

    # ODBC driver name pyodbc connects with. Default matches what the Lambda container
    # installs (Dockerfile: msodbcsql18). Dev machines commonly only have Driver 17
    # installed — override with SQLSERVER_ODBC_DRIVER=ODBC Driver 17 for SQL Server in
    # .env rather than changing this default, which would silently break Lambda.
    SQLSERVER_ODBC_DRIVER: str = "ODBC Driver 18 for SQL Server"

    # Direct connection to SG360-TECH-PRD1 (ShipperPlus host).
    # When set, get_prophecy_data() uses this instead of the SQLAPPS3 linked server.
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

    # IMAP polling — same O365 account as SMTP; set ALG_SENDER_EMAIL to Tanya's address
    IMAP_HOST: str = "outlook.office365.com"
    IMAP_PORT: int = 993
    IMAP_MAILBOX: str = "INBOX"
    ALG_SENDER_EMAIL: str = ""  # filter by sender, e.g. tanya@algworldwide.com

    INVOICE_FOLDER: str = ""  # Absolute path to watch folder; empty = disabled
    INVOICE_S3_BUCKET: str = ""  # S3 bucket for uploaded invoice PDFs; empty = disabled (mock mode, or not yet provisioned)

    APP_NAME: str = "SG360 BOL Reconciliation"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True


def _load_aws_secrets(secret_name: str) -> dict:
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def _build_database_url_from_rds_secret(secret_arn: str, host: str, port: int, database: str) -> str:
    # The RDS-managed secret (manage_master_user_password = true, aurora.tf) holds
    # ONLY {"username", "password"} — AWS generates and auto-rotates it, so whatever
    # AWSCURRENT currently holds is by definition never stale, unlike the old
    # manually-synced copy in sg360-bol-live-credentials that caused the
    # 2026-07-16 outage. host/port/database are static and come from Terraform-
    # managed Lambda env vars (DB_HOST/DB_PORT/DB_NAME), not from this secret.
    creds = _load_aws_secrets(secret_arn)
    url = URL.create(
        drivername="postgresql",
        username=creds["username"],
        password=creds["password"],
        host=host,
        port=port,
        database=database,
    )
    # url.render_as_string()/str(url) default to hide_password=True (masks the
    # password as "***") — correct for logging, wrong here, since this string
    # is consumed directly by create_engine(). Must pass False explicitly.
    return url.render_as_string(hide_password=False)


_aws_secret_name = os.environ.get("AWS_SECRET_NAME")
settings = Settings(**_load_aws_secrets(_aws_secret_name)) if _aws_secret_name else Settings()

# DB credentials always come from AWS's own auto-rotated RDS-managed secret when
# set — never from sg360-bol-live-credentials (that secret still supplies
# SMTP/SQL-Server/EIA creds above, via _aws_secret_name, unchanged). Local/
# mock-mode dev has no RDS_MASTER_SECRET_ARN env var, so settings.DATABASE_URL
# is left exactly as Settings()/.env already set it.
_rds_master_secret_arn = os.environ.get("RDS_MASTER_SECRET_ARN")
if _rds_master_secret_arn:
    settings.DATABASE_URL = _build_database_url_from_rds_secret(
        _rds_master_secret_arn,
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ["DB_NAME"],
    )
