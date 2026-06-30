from pydantic_settings import BaseSettings, SettingsConfigDict


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

    APP_NAME: str = "SG360 BOL Reconciliation"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True


settings = Settings()
