"""The JSON Schemas of config.yaml and of a project's latet.yaml, for editors and agents:
``latet config schema`` (``--project`` for the second).

Also published with the documentation, so a ``# yaml-language-server: $schema=...`` line at the top
of either file gives completion and checks in editors that read it.
"""

from __future__ import annotations

from typing import Any

SCHEMA_URL = "https://docs.latet.ai/config.schema.json"
PROJECT_SCHEMA_URL = "https://docs.latet.ai/project.schema.json"
RULES_SCHEMA_URL = "https://docs.latet.ai/rules.schema.json"


def _recognizers(schema: dict[str, Any], *paths: tuple[str, ...]) -> None:
    """Describe the recognizers at ``paths`` (properties, from the top) in full."""
    from latet.engine.detectors import checksums
    from latet.engine.detectors.recognizers import RecognizerSpec

    defs = schema.setdefault("$defs", {})
    recognizer = RecognizerSpec.model_json_schema(by_alias=True)
    defs.update(recognizer.pop("$defs", {}))
    defs["Recognizer"] = recognizer
    checksum = recognizer["properties"]["checksum"]
    names = {"enum": list(checksums.CHECKSUMS)}
    checksum["anyOf"] = [names if a == {"type": "string"} else a for a in checksum["anyOf"]]
    for path in paths:
        node = schema
        for key in path:
            node = node[key]
        node["items"] = {"$ref": "#/$defs/Recognizer"}


def config_schema() -> dict[str, Any]:
    from latet.config import Config

    schema = Config.model_json_schema()
    _recognizers(schema, ("properties", "recognizers"), ("$defs", "ProjectEntry", "properties", "recognizers"))
    properties = schema["properties"]
    review = properties["auto_mode_review"]
    properties["auto_mode_review"] = {
        "description": review.get("description", ""),
        "default": "block",
        "anyOf": [{"enum": ["block", "allow-identity"]}, {"$ref": "#/$defs/AutoModeReview"}],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_URL,
        **schema,
        "title": "Latet configuration (config.yaml)",
    }


def project_schema() -> dict[str, Any]:
    """latet.yaml: only what a repository file may set (the rest is ignored, so editors flag it)."""
    from latet.config import ProjectSettings
    from latet.projects import REPO_KEYS

    schema = ProjectSettings.model_json_schema()
    schema["properties"] = {k: v for k, v in schema["properties"].items() if k in REPO_KEYS}
    schema["additionalProperties"] = False
    _recognizers(schema, ("properties", "recognizers"))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": PROJECT_SCHEMA_URL,
        **schema,
        "title": "Latet settings for one project (latet.yaml)",
        "description": "Shared with everyone who works on the project: they can only add protection to each "
        "person's own settings.",
    }


def rules_schema() -> dict[str, Any]:
    """A rules file (``latet rules export``): what protects, the exceptions and the reviewer's guidance."""
    from latet.config import ProjectSettings
    from latet.transfer import KEYS, VERSION_KEY

    schema = ProjectSettings.model_json_schema()
    properties = schema["properties"]
    guidance = properties.pop("review_guidance")
    guidance["description"] = "Guidance for the local reviewer, added after the importer's own."
    schema["properties"] = {
        VERSION_KEY: {"type": "integer", "minimum": 1, "description": "The version of the format of this file."},
        **{k: properties[k] for k in KEYS if k in properties},
        "guidance": guidance,
    }
    schema["additionalProperties"] = False
    _recognizers(schema, ("properties", "recognizers"))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RULES_SCHEMA_URL,
        **schema,
        "title": "Latet rules (latet rules export)",
        "description": "Imported, they add protection to the importer's own rules at once; what would lower it "
        "(the exceptions, a weaker value, the guidance) is applied only when asked for, and approved.",
    }
