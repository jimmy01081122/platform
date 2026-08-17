from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export import (  # noqa: E402
    build_and_publish_export,
    build_transport_envelope,
    frame_transport_envelope,
    parse_transport_frame,
    parse_transport_envelope,
    publish_local_replay,
    verify_export,
)


def _restore(root: Path) -> None:
    if not root.exists():
        return
    for directory, directories, files in os.walk(root, topdown=True):
        path = Path(directory)
        if not path.is_symlink():
            path.chmod(0o700)
        for name in files:
            member = path / name
            if not member.is_symlink():
                member.chmod(0o600)
        for name in directories:
            member = path / name
            if not member.is_symlink():
                member.chmod(0o700)


class GateMExportTests(unittest.TestCase):
    def test_export_is_bounded_exact_set_and_excludes_weights(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "export").mkdir()
            application = project / "application"
            application.mkdir()
            (application / "m0_execution_contract.json").write_bytes(
                (
                    REPO
                    / "explorations/moe_cycle_simulator/phase7/application"
                    / "m0_execution_contract.json"
                ).read_bytes()
            )
            model = project / "model"
            model.mkdir()
            snapshot = model / "snapshot"
            snapshot.mkdir()
            (snapshot / "model-00001-of-00001.safetensors").write_bytes(
                b"weights-must-not-be-exported"
            )
            ledger = model / "model-ledger.json"
            write_new_json(ledger, {"ledger_sha256": "1" * 64})
            fixture = project / "fixture.json"
            write_new_json(
                fixture,
                {
                    "model_ledger_sha256": "1" * 64,
                    "token_ids": [],
                    "token_ids_sha256": semantic_sha256([]),
                },
            )
            receipt = project / "receipt.json"
            write_new_json(
                application / "materialization_plan.template.json",
                {
                    "storage_contract": {
                        "persistent_project_root": str(project),
                        "persistent_mount": str(project.parent),
                    },
                    "deployment": {
                        "application_target": str(application),
                        "deployment_receipt": str(receipt),
                    },
                    "paths": {
                        "snapshot": str(snapshot),
                        "model_ledger": str(ledger),
                        "capacity_prompt_fixture": str(fixture),
                    },
                },
            )
            write_new_json(
                application / "materialization_approval.template.json",
                {
                    "approved_d0_result_sha256": "3" * 64,
                    "approved_vault_mount_identity_sha256": "4" * 64,
                    "approved_ssh_host_key_sha256": "5" * 64,
                },
            )
            write_new_json(receipt, {"installation_status": "COMPLETE"})
            materialization = project / "materialization"
            (materialization / "logs").mkdir(parents=True)
            write_new_json(materialization / "evidence_ledger.json", {"test": True})
            write_new_json(
                materialization / "stage_result.json",
                {
                    "status": "COMPLETE_HARD_STOP",
                    "model_ledger_sha256": "1" * 64,
                    "capacity_prompt_fixture_sha256": file_sha256(fixture),
                },
            )
            (materialization / "logs/materialize.stdout.log").write_text(
                "bounded log\n", encoding="utf-8"
            )
            provenance = project / "provenance"
            provenance.mkdir()
            for name, value in (
                ("evidence_ledger.json", {"terminal_status": "COMPLETE"}),
                ("runtime_provenance.json", {"status": "COMPLETE"}),
                ("installed_distribution.json", {"ledger_sha256": "6" * 64}),
                ("source_tree_ledger.json", {"ledger_sha256": "7" * 64}),
            ):
                write_new_json(provenance / name, value)
            export = project / "export/gate-m"
            status = project / "export/gate-m.status"
            try:
                with self.assertRaises(M0Error):
                    build_and_publish_export(
                        application=application,
                        receipt=receipt,
                        materialization_root=materialization,
                        runtime_provenance_root=provenance,
                        export_root=export,
                        status_path=status,
                    )
                with (
                    patch(
                        "explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export.verify_install"
                    ),
                    patch(
                        "explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export.verify_materialization_terminal",
                        return_value={"terminal_status": "COMPLETE_HARD_STOP"},
                    ),
                    patch(
                        "explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export.verify_runtime_provenance",
                        return_value={"terminal_status": "COMPLETE"},
                    ),
                    patch(
                        "explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export.verify_model_ledger"
                    ),
                ):
                    manifest = build_and_publish_export(
                        application=application,
                        receipt=receipt,
                        materialization_root=materialization,
                        runtime_provenance_root=provenance,
                        export_root=export,
                        status_path=status,
                    )
                self.assertEqual(verify_export(export, status_path=status), manifest)
                paths = {item["path"] for item in manifest["members"]}
                self.assertIn("model/model_ledger.json", paths)
                self.assertNotIn("model-00001-of-00001.safetensors", "\n".join(paths))
                summary = {
                    "export_manifest_sha256": manifest["manifest_sha256"],
                    "export_commit_marker_sha256": hashlib.sha256(
                        status.read_bytes()
                    ).hexdigest(),
                }
                transport = build_transport_envelope(
                    export_root=export,
                    status_path=status,
                    remote_summary=summary,
                )
                frame = frame_transport_envelope(transport)
                self.assertEqual(parse_transport_frame(frame), transport)
                for changed_frame in (
                    frame[:-1],
                    frame + b"x",
                    frame.replace(str(len(transport)).encode(), str(len(transport) + 1).encode(), 1),
                    frame.replace(hashlib.sha256(transport).hexdigest().encode(), b"0" * 64, 1),
                ):
                    with self.subTest():
                        with self.assertRaises(M0Error):
                            parse_transport_frame(changed_frame)
                parsed = parse_transport_envelope(transport)
                self.assertEqual(parsed["remote_summary"], summary)
                local_parent = project / "local-replay"
                local_parent.mkdir()
                replay = publish_local_replay(
                    transport,
                    export_root=local_parent / "gate-m",
                    status_path=local_parent / "gate-m.status",
                )
                self.assertEqual(replay["export_manifest"], manifest)
                self.assertEqual(
                    verify_export(
                        local_parent / "gate-m",
                        status_path=local_parent / "gate-m.status",
                    ),
                    manifest,
                )
                changed = json.loads(transport)
                changed["members"][0]["payload_base64"] = "AA=="
                with self.assertRaises(M0Error):
                    parse_transport_envelope(
                        json.dumps(
                            changed,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                target = export / "model/model_ledger.json"
                target.chmod(0o600)
                target.write_text("{}\n", encoding="utf-8")
                with self.assertRaises(M0Error):
                    verify_export(export, status_path=status)
            finally:
                _restore(project)


if __name__ == "__main__":
    unittest.main()
