from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./support_agent.db"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    demo_reviewer_token: str = "local-reviewer-token-change-me"
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()
