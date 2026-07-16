from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from core.trading.models import TelegramUser


class Command(BaseCommand):
    help = (
        "Add (or upgrade to admin) a Telegram chat_id allowed to control "
        "the bot."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "chat_id", type=int, help="Telegram chat_id of the admin user"
        )
        parser.add_argument("--label", default="", help="Free-text label")

    def handle(self, *args: Any, **options: Any) -> None:
        chat_id: int = options["chat_id"]
        label: str = options["label"]
        user, created = TelegramUser.objects.update_or_create(
            chat_id=chat_id,
            defaults={"is_admin": True, "label": label},
        )
        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} admin: {user}"))
