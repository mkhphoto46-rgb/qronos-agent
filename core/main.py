from __future__ import annotations

import sys
from datetime import datetime


APP_NAME = "Qronos"


def get_greeting() -> str:
    """Return a simple local greeting."""
    current_hour = datetime.now().hour

    if current_hour < 12:
        return "Good morning."
    if current_hour < 18:
        return "Good afternoon."
    return "Good evening."


def main() -> int:
    """Run the minimal Qronos core."""
    print("=" * 50)
    print(f"{APP_NAME} Core")
    print("=" * 50)
    print(get_greeting())
    print("Qronos is running.")
    print("Type 'exit' to stop.")

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nQronos stopped safely.")
            return 0

        if user_input.lower() == "exit":
            print("Qronos stopped safely.")
            return 0

        if not user_input:
            continue

        print(f"Qronos: I received: {user_input}")


if __name__ == "__main__":
    sys.exit(main())