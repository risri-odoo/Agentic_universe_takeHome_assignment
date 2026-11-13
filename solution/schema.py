"""Load the workflow lead schemas and the disposition catalogue from assignment-a-data/schemas."""
import csv
import json
from pathlib import Path

from .normalize import normalize_header

# solution/ sits next to assignment-a-data/ at the repo root.
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


class WorkflowSchema:
    """One workflow's lead schema plus a few lookups we need while ingesting."""

    def __init__(self, data):
        self.workflow_id = data["workflow_id"]
        self.fields = data["fields"]  # list of dicts, kept in schema order

        # The field of type 'phone' is the lead identity; the 'reference' field is external_ref.
        self.phone_field = next(f["name"] for f in self.fields if f["type"] == "phone")
        self.reference_field = next(f["name"] for f in self.fields if f.get("reference"))

        # normalised header text -> field name (from the name and every alias)
        self.header_lookup = {}
        for field in self.fields:
            for label in [field["name"]] + field.get("aliases", []):
                self.header_lookup[normalize_header(label)] = field["name"]

    def map_headers(self, headers):
        """Return {column_index: field_name}. Unknown columns are ignored.

        If two columns map to the same field, the first (left-most) one wins.
        """
        mapping = {}
        used = set()
        for index, header in enumerate(headers):
            field_name = self.header_lookup.get(normalize_header(header))
            if field_name and field_name not in used:
                mapping[index] = field_name
                used.add(field_name)
        return mapping


def load_schemas(schema_dir=SCHEMA_DIR):
    """Return {workflow_id: WorkflowSchema} for every *.json file in the schema folder."""
    schemas = {}
    for path in sorted(Path(schema_dir).glob("*.json")):
        with open(path, encoding="utf-8") as f:
            schema = WorkflowSchema(json.load(f))
        schemas[schema.workflow_id] = schema
    return schemas


def load_dispositions(schema_dir=SCHEMA_DIR):
    """Return {(workflow_id, code): {"is_terminal": bool, "is_success": bool}}."""
    catalogue = {}
    with open(Path(schema_dir) / "dispositions.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            catalogue[(row["workflow_id"], row["code"])] = {
                "is_terminal": row["is_terminal"].strip().lower() == "true",
                "is_success": row["is_success"].strip().lower() == "true",
            }
    return catalogue
