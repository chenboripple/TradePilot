from .database import init_database


def main() -> None:
    path = init_database()
    print(f"TradePilot SQLite ready: {path}")


if __name__ == "__main__":
    main()
