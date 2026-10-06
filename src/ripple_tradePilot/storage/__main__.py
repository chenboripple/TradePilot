from .database import DATABASE_SCHEMA_VERSION, check_database_integrity, init_database


def main() -> None:
    path = check_database_integrity()
    init_database()
    print(f"TradePilot SQLite schema v{DATABASE_SCHEMA_VERSION} ready: {path}")


if __name__ == "__main__":
    main()
