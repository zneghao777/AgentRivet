"""通用 Skill 导入与安全边界测试。"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.skills.importer import (
    SkillImportError,
    UploadedSkillFile,
    install_uploaded_skills,
)

AUTH = {"X-API-Key": "test-key-1"}


def _manifest(name: str, description: str = "导入测试 Skill") -> bytes:
    return (
        f"---\nname: {name}\ndescription: {description}\n"
        "version: 1.2.0\ntags: [test, imported]\n---\n\n# Instructions\n"
    ).encode()


def test_import_single_markdown(tmp_path):
    result = install_uploaded_skills(
        [UploadedSkillFile("example.md", "example.md", _manifest("single-md"))],
        external_root=tmp_path,
        existing={},
        overwrite=False,
    )

    assert [skill.name for skill in result.imported] == ["single-md"]
    assert (tmp_path / "single-md" / "SKILL.md").exists()


def test_import_folder_preserves_resources_and_warns_for_scripts(tmp_path):
    files = [
        UploadedSkillFile("SKILL.md", "folder-skill/SKILL.md", _manifest("folder-skill")),
        UploadedSkillFile("guide.txt", "folder-skill/references/guide.txt", b"reference"),
        UploadedSkillFile("run.py", "folder-skill/scripts/run.py", b"def run(): return 'ok'"),
    ]
    result = install_uploaded_skills(
        files,
        external_root=tmp_path,
        existing={},
        overwrite=False,
    )

    assert (tmp_path / "folder-skill" / "references" / "guide.txt").read_text() == "reference"
    assert result.warnings == ["包含 Python 脚本；启用前请确认代码来源可信"]


def test_import_rejects_zip_path_traversal(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../SKILL.md", _manifest("unsafe"))

    with pytest.raises(SkillImportError, match="路径不安全"):
        install_uploaded_skills(
            [UploadedSkillFile("unsafe.zip", "unsafe.zip", stream.getvalue())],
            external_root=tmp_path,
            existing={},
            overwrite=False,
        )


def test_import_zip_ignores_macos_metadata(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("__MACOSX/._SKILL.md", b"metadata")
        archive.writestr("archive-skill/SKILL.md", _manifest("archive-skill"))
        archive.writestr("archive-skill/references/readme.txt", b"reference")

    result = install_uploaded_skills(
        [UploadedSkillFile("archive.zip", "archive.zip", stream.getvalue())],
        external_root=tmp_path,
        existing={},
        overwrite=False,
    )

    assert [skill.name for skill in result.imported] == ["archive-skill"]
    assert (tmp_path / "archive-skill" / "references" / "readme.txt").exists()


@pytest.mark.asyncio
async def test_import_and_delete_skill_api(app_client, db_session, tmp_path, monkeypatch):
    import app.api.router_skills as router_skills
    import app.skills.manager as manager_module
    from app.skills.manager import SkillManager

    manager = SkillManager()
    monkeypatch.setattr(manager_module, "EXTERNAL_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(router_skills, "EXTERNAL_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(router_skills, "skill_manager", manager)
    await manager.discover(db_session)

    response = await app_client.post(
        "/api/v1/skills/import",
        headers=AUTH,
        files={"files": ("api-skill.md", _manifest("api-skill"), "text/markdown")},
        data={"relative_paths": json.dumps(["api-skill.md"])},
    )

    assert response.status_code == 200, response.text
    imported = response.json()["data"]["skills"][0]
    assert imported["name"] == "api-skill"
    assert imported["source"] == "external"
    assert imported["enabled"] is False

    delete_response = await app_client.delete("/api/v1/skills/api-skill", headers=AUTH)
    assert delete_response.status_code == 200
    assert not (tmp_path / "api-skill").exists()


@pytest.mark.asyncio
async def test_enable_script_skill_rebuilds_runtime_tools(
    app_client, db_session, tmp_path, monkeypatch
):
    import app.api.router_skills as router_skills
    import app.skills.manager as manager_module
    from app.harness.app_runtime import harness_app
    from app.skills.manager import SkillManager
    from app.tools.registry import tool_registry

    manager = SkillManager()
    monkeypatch.setattr(manager_module, "EXTERNAL_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(router_skills, "EXTERNAL_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(router_skills, "skill_manager", manager)
    await manager.discover(db_session)

    script = b'DESCRIPTION = "QA tool"\n\ndef run(text: str) -> str:\n    return text\n'
    response = await app_client.post(
        "/api/v1/skills/import",
        headers=AUTH,
        files=[
            ("files", ("SKILL.md", _manifest("script-skill"), "text/markdown")),
            ("files", ("run.py", script, "text/x-python")),
        ],
        data={
            "relative_paths": json.dumps(
                ["script-skill/SKILL.md", "script-skill/scripts/run.py"]
            )
        },
    )
    assert response.status_code == 200, response.text
    tool_name = "skill_script-skill__run"
    assert tool_registry.get(tool_name) is None

    graph_before = harness_app.graph
    enable_response = await app_client.post(
        "/api/v1/skills/script-skill/enable", headers=AUTH
    )
    assert enable_response.status_code == 200
    assert tool_registry.get(tool_name) is not None
    assert harness_app.graph is not graph_before

    disable_response = await app_client.post(
        "/api/v1/skills/script-skill/disable", headers=AUTH
    )
    assert disable_response.status_code == 200
    assert tool_registry.get(tool_name) is None

    await app_client.delete("/api/v1/skills/script-skill", headers=AUTH)
