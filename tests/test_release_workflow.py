from __future__ import annotations

from pathlib import Path


def test_hub_publish_workflow_builds_the_serving_lifecycle_image_contract():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "publish-hub-image.yml").read_text(
        encoding="utf-8"
    )

    assert "packages: write" in workflow
    assert "deploy/Dockerfile.hub" in workflow
    assert "ghcr.io" in workflow
    assert "${{ github.repository }}" in workflow
    assert "type=raw,value=latest,enable={{is_default_branch}}" in workflow
    assert "push: true" in workflow
    assert "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10" in workflow
    assert "actions/attest@36051bcae73b7c2a8a6945a48cbf80953c6baa35" in workflow
