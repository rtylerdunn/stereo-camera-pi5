import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.json"


def load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return json.load(f)


config = load_config()
