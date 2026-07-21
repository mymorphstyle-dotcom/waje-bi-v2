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
        if _looks_non_mapping_yaml(text):
            raise ValueError(f"{contract_path} must contain a mapping at top level")
        raise ModuleNotFoundError(
            f"PyYAML is required to load contract: {contract_path}"
        )
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{contract_path} must contain a mapping at top level")
    return data


def _looks_non_mapping_yaml(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return False
    lowered = stripped.lower()
    return stripped[0] in "-[0123456789" or lowered.startswith(
        ("false", "true", "null", "~")
    )
