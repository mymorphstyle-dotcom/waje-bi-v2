from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_legacy_artifact_read_and_export_routes_are_removed() -> None:
    artifact_root = ROOT / "app" / "api" / "artifacts"
    store = (ROOT / "app/api/_conversationStore.ts").read_text(encoding="utf-8")

    assert not any(artifact_root.rglob("route.ts"))
    assert "readArtifact" not in store
    assert "ArtifactRecord" not in store
    assert "artifact_publication_missing" not in store
