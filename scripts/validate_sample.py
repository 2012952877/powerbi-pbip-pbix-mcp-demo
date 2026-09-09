"""Optional online schema check against Microsoft's public schemas, not a conversion."""

import json
import urllib.request
from functools import lru_cache
from urllib.parse import urlsplit

from jsonschema import Draft7Validator
from referencing import Registry, Resource

from pbip_mcp.synthetic import fixture_files


@lru_cache(maxsize=128)
def retrieve(uri: str) -> Resource:
    parts = urlsplit(uri)
    if parts.scheme != "https" or parts.netloc != "developer.microsoft.com" or not parts.path.startswith("/json-schemas/fabric/"):
        raise ValueError("Only the fixture's official Microsoft schema namespace is permitted.")
    path = parts.path.removeprefix("/json-schemas/")
    url = "https://raw.githubusercontent.com/microsoft/json-schemas/main/" + path
    with urllib.request.urlopen(url, timeout=30) as response:
        schema = json.load(response)
    return Resource.from_contents(schema)


def main() -> None:
    registry = Registry(retrieve=retrieve)
    count = 0
    for name, content in fixture_files().items():
        if name.endswith(".tmdl"):
            continue
        document = json.loads(content)
        schema = retrieve(document["$schema"]).contents
        validator = Draft7Validator(schema, registry=registry)
        errors = list(validator.iter_errors(document))
        if errors:
            for error in errors:
                print(f"SCHEMA_ERROR {name} {list(error.absolute_path)}: {error.message}")
            raise SystemExit(1)
        count += 1
        print("SCHEMA_OK " + name)
    print(f"{count} JSON definitions match official schemas. TMDL/Desktop opening still requires the real worker.")


if __name__ == "__main__":
    main()
