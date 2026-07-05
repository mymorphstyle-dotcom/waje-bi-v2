from pathlib import Path
from typing import Any, Union

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


def load_contract(path: Union[str, Path]) -> dict[str, Any]:
    contract_path = Path(path)
    text = contract_path.read_text(encoding="utf-8")
    if yaml is None:
        if text.lstrip().startswith("-"):
            raise ValueError(f"{contract_path} must contain a mapping at top level")
        raise ModuleNotFoundError(f"PyYAML is required to load contract: {contract_path}")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{contract_path} must contain a mapping at top level")
    return data
