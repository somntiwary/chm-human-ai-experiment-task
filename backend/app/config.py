from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ExperimentConfig:
    participants: tuple[str, ...]
    discussion_seconds: int


def load_experiment_config(path: Path) -> ExperimentConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"Could not load experiment config at {path}") from error

    if not isinstance(data, dict) or data.keys() != {"participants", "discussion_seconds"}:
        raise ValueError(f"{path} must contain participants and discussion_seconds")

    participants = data["participants"]
    if (
        not isinstance(participants, list)
        or len(participants) != 3
        or any(kind not in ("human", "ai") for kind in participants)
        or "human" not in participants
    ):
        raise ValueError(
            f"{path}: participants must be three humans, two humans and one AI, "
            "or one human and two AIs"
        )

    seconds = data["discussion_seconds"]
    if type(seconds) is not int or not 1 <= seconds <= 2_147_483_647:
        raise ValueError(f"{path}: discussion_seconds must be a positive 32-bit integer")

    return ExperimentConfig(tuple(participants), seconds)
