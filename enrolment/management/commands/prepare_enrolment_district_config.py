"""Prepare district schema additions in an exported JSON file only."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enrolment.district_config import prepare_export


class Command(BaseCommand):
    help = "Prepare GROUP district schema additions in a new JSON export; no database writes."
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("input", type=Path, help="Exported Social Protection configuration and phases JSON")
        parser.add_argument("output", type=Path, help="New output file; existing files are never overwritten")
        parser.add_argument("--phase-id", action="append", default=[],
                            help="Existing GROUP phase ID to update; repeat for multiple phases")

    def handle(self, *args, **options):
        try:
            source = Path(options["input"])
            output = Path(options["output"])
            prepared = prepare_export(json.loads(source.read_text(encoding="utf-8")), options["phase_id"])
            # Validate and serialize before creating any output.
            content = json.dumps(prepared, indent=2, ensure_ascii=False) + "\n"
            with output.open("x", encoding="utf-8") as target:
                target.write(content)
        except (ValueError, TypeError, OSError) as error:
            raise CommandError(f"Cannot prepare district configuration: {error}") from error
        self.stdout.write(self.style.SUCCESS(f"Prepared {output}; no database changes applied."))
