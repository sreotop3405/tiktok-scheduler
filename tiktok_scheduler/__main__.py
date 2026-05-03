"""Allow ``python -m tiktok_scheduler`` to invoke the CLI."""

from .cli import app

if __name__ == "__main__":
    app()
