from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./foodbot.db"

    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "change-me"
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"

    delivery_fee: Decimal = Decimal("1000")
    bank_name: str = ""
    bank_account_number: str = ""
    bank_account_name: str = ""

    brand_name: str = "Tofex Grill and More"
    outlet_address: str = "[Outlet address - to be added]"
    outlet_phone: str = "[Outlet phone - to be added]"
    support_phone: str = "[Support phone - to be added]"
    refund_timeframe: str = "[timeframe - to be added]"
    maps_region_hint: str = "Lagos, Nigeria"
    checkout_reminder_minutes: int = 10
    checkout_cancel_minutes: int = 30
    address_edit_timeout_minutes: int = 10
    thank_you_delay_minutes: int = 10
    rider_pickup_reminder_minutes: int = 10
    rider_window_warning_hours: int = 23
    overdue_grace_minutes: int = 10

    # Print outgoing messages instead of sending them, and expose /dev/outbox for the simulator.
    dry_run: bool = False
    staff_username: str = "admin"
    staff_password: str = ""
    session_secret: str = ""

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
