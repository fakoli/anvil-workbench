"""Explicit, bridge-local Codex skill discovery.

Workbench never asks a browser or model for a filesystem path.  An operator
starts a bridge with one or more local skill roots; the bridge discovers only
``SKILL.md`` files below those roots, reports metadata to the hub, and may add
the selected reviewed instructions to a Codex work packet.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class SkillError(RuntimeError):
    """An approved skill could not be resolved from the bridge's local registry."""


_MAX_SKILL_BYTES = 128_000
_MAX_SKILLS = 128
_FRONTMATTER = re.compile(r"\A---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)\Z", re.DOTALL)
_FIELD = re.compile(r"^(?P<key>name|description):\s*[\"']?(?P<value>.*?)[\"']?\s*$", re.MULTILINE)
_SKILL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9:_-]{0,119}$")


@dataclass(frozen=True)
class LocalSkill:
    skill_id: str
    description: str
    content_sha256: str
    instructions: str
    path: Path

    def metadata(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "description": self.description,
            "content_sha256": self.content_sha256,
        }


class SkillRegistry:
    """Read a small, explicit local skill set without exposing its paths to the hub."""

    def __init__(self, roots: Iterable[Path] = ()) -> None:
        self.roots = tuple(root.resolve() for root in roots)

    @staticmethod
    def _parse(path: Path) -> LocalSkill:
        raw = path.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > _MAX_SKILL_BYTES:
            raise SkillError(f"skill is too large: {path.name}")
        match = _FRONTMATTER.match(raw)
        if match is None:
            raise SkillError(f"skill is missing YAML frontmatter: {path.name}")
        fields = {item.group("key"): item.group("value").strip() for item in _FIELD.finditer(match.group("meta"))}
        skill_id = fields.get("name", "").strip()
        description = fields.get("description", "").strip()
        if not _SKILL_ID.fullmatch(skill_id):
            raise SkillError(f"skill has an invalid name: {path.name}")
        if not description or len(description) > 500:
            raise SkillError(f"skill needs a short description: {path.name}")
        body = match.group("body").strip()
        if not body:
            raise SkillError(f"skill has no instructions: {path.name}")
        return LocalSkill(
            skill_id=skill_id,
            description=description,
            content_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            instructions=body,
            path=path,
        )

    def discover(self) -> dict[str, LocalSkill]:
        discovered: dict[str, LocalSkill] = {}
        for root in self.roots:
            if not root.is_dir():
                raise SkillError(f"configured skill root is unavailable: {root}")
            for path in sorted(root.rglob("SKILL.md")):
                if len(discovered) >= _MAX_SKILLS:
                    raise SkillError("bridge skill limit exceeded")
                skill = self._parse(path)
                if skill.skill_id in discovered:
                    raise SkillError(f"duplicate configured skill name: {skill.skill_id}")
                discovered[skill.skill_id] = skill
        return discovered

    def selected(self, skill_ids: Iterable[str]) -> tuple[LocalSkill, ...]:
        available = self.discover()
        selected: list[LocalSkill] = []
        for skill_id in skill_ids:
            skill = available.get(skill_id)
            if skill is None:
                raise SkillError(f"requested skill is not configured on this bridge: {skill_id}")
            selected.append(skill)
        return tuple(selected)
