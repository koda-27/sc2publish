from types import SimpleNamespace

import pytest

from sc2publish.config import PublishError
from sc2publish.connection import Native


def test_prepublish_looks_up_current_and_previous_names():
    sent = []
    native = SimpleNamespace(
        project=SimpleNamespace(name="New Name", previous_names=["Old Name"]),
        next_token=lambda: 7,
        send_record=lambda *args: sent.append(args),
        correlated=lambda *args: {"m_result": {"choice": 0, "value": {"ok": True}}},
    )
    assert Native.prepublish(native) == {"ok": True}
    assert sent[0][3]["m_mapNames"] == ["New Name", "Old Name"]


def test_previous_names_validation(tmp_path):
    from sc2publish.config import Project
    layer = tmp_path / "Map.SC2Map"
    layer.mkdir()
    manifest = tmp_path / "p.toml"
    manifest.write_text('[project]\nname = "New"\nprevious_names = [""]\n[build]\nlayers = ["Map.SC2Map"]\noutput = "out"\n')
    with pytest.raises(PublishError):
        Project(manifest)


def _project(**project):
    from types import SimpleNamespace as NS
    defaults = {"map_id": 0, "namespace": 0}
    defaults.update({k: v for k, v in project.items() if k in defaults})
    return NS(project=project, **defaults)


def test_create_takes_allocated_namespace_and_refuses_existing_listing():
    from sc2publish.publisher import verify_identity
    project = _project(create=True)
    assert verify_identity(project, {"m_priorRevision": None, "m_attributeNs": 339681}, create=True) is None
    assert project.namespace == 339681
    with pytest.raises(PublishError):
        verify_identity(_project(create=True), {"m_priorRevision": {"m_mapId": 1}, "m_attributeNs": 5}, create=True)
    with pytest.raises(PublishError):
        verify_identity(_project(create=True, map_id=268504), {"m_priorRevision": None, "m_attributeNs": 5}, create=True)


def test_missing_listing_without_create_is_refused():
    from sc2publish.publisher import verify_identity
    with pytest.raises(PublishError, match="create"):
        verify_identity(_project(), {"m_priorRevision": None, "m_attributeNs": 5})
