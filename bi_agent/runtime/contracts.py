from pathlib import Path
from typing import Any, Union

import yaml


def load_contract(path: Union[str, Path]) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
