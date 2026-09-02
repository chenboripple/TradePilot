from .database import DATABASE_SCHEMA_VERSION, init_database


def main() -> None:
    path = init_database()
    print(f"TradePilot SQLite schema v{DATABASE_SCHEMA_VERSION} ready: {path}")


if __name__ == "__main__":
    main()
