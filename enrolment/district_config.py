"""Prepare an offline, reviewable configuration export; never connect to a database."""
import copy
import json


def add_district_property(schema):
    if isinstance(schema, str):
        schema = json.loads(schema)
    if not isinstance(schema, dict):
        raise ValueError("Beneficiary schema must be a JSON object.")
    result = copy.deepcopy(schema)
    if result.get("type", "object") != "object":
        raise ValueError("Beneficiary schema must have object type.")
    result.setdefault("type", "object")
    properties = result.setdefault("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("Schema properties must be an object.")
    if "district_id" in properties:
        if not isinstance(properties["district_id"], dict) or properties["district_id"].get("type") != "integer":
            raise ValueError("Existing district_id conflicts with the reserved integer filter.")
    else:
        properties["district_id"] = {"type": "integer"}
    return result


def prepare_export(export, phase_ids):
    result = copy.deepcopy(export)
    if not isinstance(result, dict) or not isinstance(result.get("module_config"), dict):
        raise ValueError("Export must contain a module_config object and phases list.")
    defaults = result["module_config"].setdefault("benefit_plan_creation_defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("benefit_plan_creation_defaults must be an object.")
    group = defaults.setdefault("GROUP", {})
    if not isinstance(group, dict):
        raise ValueError("GROUP creation defaults must be an object.")
    # Existing common/type criteria and all other module configuration stay intact.
    group["beneficiary_data_schema"] = add_district_property(group.get("beneficiary_data_schema", {}))
    phases = result.get("phases")
    if not isinstance(phases, list):
        raise ValueError("Export must contain a phases list.")
    selected = {str(value) for value in phase_ids}
    found = set()
    for phase in phases:
        if not isinstance(phase, dict):
            raise ValueError("Each phase must be an object.")
        identifier = str(phase.get("id"))
        if identifier not in selected:
            continue
        if identifier in found:
            raise ValueError("Duplicate phase ID in export: " + identifier)
        if phase.get("type") != "GROUP":
            raise ValueError("Selected phase is not a GROUP phase: " + identifier)
        found.add(identifier)
        phase["beneficiary_data_schema"] = add_district_property(({} if phase.get("beneficiary_data_schema") is None else phase["beneficiary_data_schema"]))
    if found != selected:
        raise ValueError("Unknown phase IDs: " + ", ".join(sorted(selected - found)))
    return result
