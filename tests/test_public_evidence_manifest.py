from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_public_evidence import EVIDENCE, MANIFEST, main


def test_public_evidence_manifest_matches_curated_files() -> None:
    assert main() == 0


def test_public_evidence_manifest_fails_for_unlisted_file(tmp_path: Path, monkeypatch) -> None:
    evidence = tmp_path / "public"
    evidence.mkdir()
    (evidence / "listed.json").write_text("{}", encoding="utf-8")
    (evidence / "extra.log").write_text("not published", encoding="utf-8")
    manifest = evidence / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "listed.json",
                        "sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
                        "bytes": 2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.verify_public_evidence.EVIDENCE", evidence)
    monkeypatch.setattr("scripts.verify_public_evidence.MANIFEST", manifest)
    assert main() == 1
