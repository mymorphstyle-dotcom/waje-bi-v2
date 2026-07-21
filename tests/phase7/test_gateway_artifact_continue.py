from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_legacy_artifact_continue_producer_is_removed() -> None:
    app_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.ts*")
    )

    assert "artifact_continue" not in app_sources
    assert "requireArtifactForContinue" not in app_sources
    assert "/api/artifacts" not in app_sources
    assert 'producerKind: "thread_message";' in app_sources
