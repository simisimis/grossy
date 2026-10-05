"""Strict YAML loading for Grossy configuration (PRD section 7.3).

Rejects duplicate YAML keys with precise key paths and wraps Pydantic
validation errors into ``GrossyConfigError`` messages that carry exact
validation paths (PRD section 18: "show exact validation path").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from grossy.config.models import GrossyConfigModel


class GrossyConfigError(Exception):
    """Configuration error with precise validation paths."""


class DuplicateYamlKeyError(GrossyConfigError):
    """Duplicate YAML key (PRD 7.3.15)."""


def _format_key(key: object) -> str:
    return key if isinstance(key, str) else repr(key)


class _StrictYamlLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PRD 7.3.15)."""

    def __init__(self, stream: str | bytes) -> None:
        super().__init__(stream)
        self._grossy_key_stack: list[str] = []

    def _construct_unique_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)  # type: ignore[no-untyped-call]
            if not isinstance(key, str):
                # Non-string keys (for example unhashable lists from
                # ``? [a, b]``) would otherwise escape configuration error
                # handling with a raw TypeError; report them by location
                # instead (PRD section 18: exact validation paths).
                location = " > ".join((*self._grossy_key_stack, repr(key)))
                raise GrossyConfigError(
                    f"invalid YAML mapping key at {location!r}: mapping keys must be strings"
                )
            if key in mapping:
                path = " > ".join((*self._grossy_key_stack, _format_key(key)))
                raise DuplicateYamlKeyError(f"duplicate YAML key {path!r}")
            self._grossy_key_stack.append(_format_key(key))
            try:
                mapping[key] = self.construct_object(value_node, deep=deep)  # type: ignore[no-untyped-call]
            finally:
                self._grossy_key_stack.pop()
        return mapping


def _unique_mapping_constructor(
    loader: Any, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = loader._construct_unique_mapping(node, deep)
    return mapping


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _unique_mapping_constructor,
)


def format_validation_errors(exc: ValidationError) -> str:
    """Render Pydantic validation errors with exact dot-joined paths."""
    lines = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"config.{loc}: {error['msg']}")
    return "configuration validation failed:\n" + "\n".join(lines)


def load_config_text(text: str) -> GrossyConfigModel:
    """Parse and strictly validate a Grossy YAML configuration document."""
    try:
        data = yaml.load(text, Loader=_StrictYamlLoader)
    except DuplicateYamlKeyError as exc:
        raise GrossyConfigError(str(exc)) from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise GrossyConfigError(f"invalid YAML{location}; check syntax") from exc
    if data is None:
        raise GrossyConfigError("configuration is empty")
    if not isinstance(data, dict):
        raise GrossyConfigError("configuration must be a YAML mapping")
    try:
        return GrossyConfigModel.model_validate(data)
    except ValidationError as exc:
        raise GrossyConfigError(format_validation_errors(exc)) from exc


def load_config_file(path: str | Path) -> GrossyConfigModel:
    """Load and strictly validate a Grossy YAML configuration file."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GrossyConfigError(
            f"cannot read configuration file {file_path}: {exc.strerror or exc}"
        ) from exc
    return load_config_text(text)
