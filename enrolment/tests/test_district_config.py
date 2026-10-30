import copy
import unittest

from enrolment.district_config import prepare_export


class DistrictConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.export = {
            "module_config": {
                "unrelated": True,
                "benefit_plan_creation_defaults": {
                    "common": {"beneficiary_data_schema": {"properties": {"validation_status": {"type": "string"}}}},
                    "GROUP": {"json_ext": {"advanced_criteria": {"ACTIVE": [{"field": "validation_status", "value": "VERIFIED"}]}}},
                },
            },
            "phases": [
                {"id": "a", "type": "GROUP", "beneficiary_data_schema": {"properties": {"score": {"type": "integer"}}}, "json_ext": {"preserved": True}},
                {"id": "b", "type": "INDIVIDUAL", "beneficiary_data_schema": {}},
            ],
        }

    def test_targeted_merge_preserves_criteria_and_is_idempotent(self):
        original = copy.deepcopy(self.export)
        result = prepare_export(self.export, ["a"])
        self.assertEqual(self.export, original)
        self.assertEqual(result["phases"][0]["beneficiary_data_schema"]["properties"],
                         {"score": {"type": "integer"}, "district_id": {"type": "integer"}})
        self.assertEqual(result["phases"][0]["json_ext"], original["phases"][0]["json_ext"])
        self.assertEqual(result["phases"][1], original["phases"][1])
        defaults = result["module_config"]["benefit_plan_creation_defaults"]
        self.assertEqual(defaults["common"], original["module_config"]["benefit_plan_creation_defaults"]["common"])
        self.assertEqual(defaults["GROUP"]["json_ext"], original["module_config"]["benefit_plan_creation_defaults"]["GROUP"]["json_ext"])
        self.assertEqual(result, prepare_export(result, ["a"]))

    def test_no_phase_ids_changes_only_creation_defaults(self):
        self.assertEqual(prepare_export(self.export, [])["phases"], self.export["phases"])

    def test_unknown_individual_and_conflicting_fields_fail(self):
        for ids in (["unknown"], ["b"]):
            with self.assertRaises(ValueError):
                prepare_export(self.export, ids)
        self.export["phases"][0]["beneficiary_data_schema"] = {"properties": {"district_id": {"type": "string"}}}
        with self.assertRaises(ValueError):
            prepare_export(self.export, ["a"])

    def test_string_schema_and_existing_metadata_preserved(self):
        self.export["phases"][0]["beneficiary_data_schema"] = '{"properties":{"district_id":{"type":"integer","description":"keep"}}}'
        result = prepare_export(self.export, ["a"])
        self.assertEqual(result["phases"][0]["beneficiary_data_schema"]["properties"]["district_id"]["description"], "keep")



class DistrictConfigurationCommandTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'before.json'
        self.output = Path(self.directory.name) / 'proposed.json'
        self.export = {
            'module_config': {},
            'phases': [
                {'id': 'a', 'type': 'GROUP', 'beneficiary_data_schema': {}},
                {'id': 'b', 'type': 'GROUP', 'beneficiary_data_schema': {}},
            ],
        }
        self.write_source(self.export)

    def write_source(self, value):
        import json
        self.source.write_text(json.dumps(value), encoding='utf-8')

    def run_command(self, *extra):
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command
        from enrolment.management.commands.prepare_enrolment_district_config import Command
        # The command must neither run framework database checks nor access a cursor.
        with patch.object(Command, 'check', side_effect=AssertionError('system checks')), \
             patch.object(Command, 'check_migrations', side_effect=AssertionError('migration checks')), \
             patch('django.db.backends.base.base.BaseDatabaseWrapper.cursor', side_effect=AssertionError('database access')):
            call_command(Command(), str(self.source), str(self.output), *extra,
                         stdout=StringIO())

    def test_command_targets_multiple_phases_and_preserves_input(self):
        import json
        original = self.source.read_bytes()
        self.run_command('--phase-id', 'a', '--phase-id', 'b')
        result = json.loads(self.output.read_text())
        self.assertEqual(result, prepare_export(self.export, ['a', 'b']))
        self.assertEqual(self.source.read_bytes(), original)

    def test_command_without_ids_only_changes_creation_defaults(self):
        import json
        self.run_command()
        result = json.loads(self.output.read_text())
        self.assertEqual(result['phases'], self.export['phases'])
        self.assertEqual(result['module_config']['benefit_plan_creation_defaults']['GROUP']
                         ['beneficiary_data_schema']['properties']['district_id'], {'type': 'integer'})

    def test_existing_output_and_input_path_cannot_be_overwritten(self):
        from django.core.management.base import CommandError
        self.output.write_text('keep', encoding='utf-8')
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(self.output.read_text(), 'keep')
        self.output = self.source
        original = self.source.read_bytes()
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(self.source.read_bytes(), original)

    def test_invalid_json_unknown_phase_and_missing_input_create_no_output(self):
        from django.core.management.base import CommandError
        self.source.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertFalse(self.output.exists())
        self.write_source(self.export)
        with self.assertRaises(CommandError):
            self.run_command('--phase-id', 'missing')
        self.assertFalse(self.output.exists())
        self.source.unlink()
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertFalse(self.output.exists())

    def test_duplicate_phase_and_malformed_containers_are_rejected(self):
        from django.core.management.base import CommandError
        invalid = [
            {'module_config': {'benefit_plan_creation_defaults': []}, 'phases': []},
            {'module_config': {'benefit_plan_creation_defaults': {'GROUP': []}}, 'phases': []},
            {'module_config': {}, 'phases': [self.export['phases'][0]] * 2},
            {'module_config': {}, 'phases': [{'id': 'a', 'type': 'GROUP', 'beneficiary_data_schema': []}]},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.write_source(value)
                with self.assertRaises(CommandError):
                    self.run_command('--phase-id', 'a')
                self.assertFalse(self.output.exists())
