import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.json"


def load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)


config = load_config()
