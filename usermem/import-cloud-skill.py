"""cloud-skills 懒导入：把 /cloud-skills 中的技能包导入 Dify（skills 表）并绑定到综合 agent。

用法（api 容器内）：
  python3 import_cloud_skill.py --skill <id> [--skill <id2> ...]

行为：
  - 从 /cloud-skills/manifest.json 读取元数据（描述/分类），zip 按文件名 <id>.zip 定位
  - 规范化包：保证根级 SKILL.md（小写 skill.md 改名，缺失则合成），统一重写
    YAML frontmatter（name=<id>，description=manifest 描述）
  - 经 SkillPackageService.validate_and_normalize 校验后写 storage + skills/skill_versions
  - 绑定到综合 agent：AgentSkillBinding（草稿，随未来发布携带）+ 当前活跃快照
  - 幂等：技能已存在时只补绑定，不重复导入

仅在 api 容器内运行（依赖 app_factory / models / storage）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import os
import sys
import zipfile

from sqlalchemy import select

CLOUD_ROOT = os.environ.get("CLOUD_SKILLS_ROOT", "/cloud-skills")
DEFAULT_AGENT_ID = "01a08f72-8f58-7d0e-a657-172aea7f11d9"

FRONTMATTER_MAX_DESC = 1024


def _frontmatter(name: str, description: str) -> str:
    # json.dumps 产出的带引号单行字符串是合法 YAML，且安全转义特殊字符
    desc = json.dumps(description[:FRONTMATTER_MAX_DESC], ensure_ascii=False)
    return f"---\nname: {name}\ndescription: {desc}\n---\n"


def _load_manifest() -> dict[str, dict]:
    path = os.path.join(CLOUD_ROOT, "manifest.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {s["id"]: s for s in data.get("skills", []) if isinstance(s, dict) and s.get("id")}
    except Exception:
        return {}


def _zip_index() -> dict[str, str]:
    """id -> zip 路径（全量扫描一次，2063 个文件可接受）"""
    index: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(CLOUD_ROOT):
        for f in files:
            if f.endswith(".zip"):
                index[f[:-4]] = os.path.join(dirpath, f)
    return index


def _normalize_zip(src: bytes, name: str, description: str, display_name: str) -> bytes:
    """保证根级 SKILL.md 并重写 frontmatter，返回规范化后的 zip 字节。"""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        names = set(zin.namelist())
        skill_md_key = None
        if "SKILL.md" in names:
            skill_md_key = "SKILL.md"
        elif "skill.md" in names:
            skill_md_key = "skill.md"
        body = ""
        if skill_md_key:
            raw = zin.read(skill_md_key).decode("utf-8", errors="replace")
            # 剥掉已有 frontmatter（统一以脚本生成的为准）
            if raw.startswith("---"):
                parts = raw.split("\n---", 2)
                if len(parts) >= 2:
                    rest = parts[1]
                    body = rest[1:] if rest.startswith("\n") else rest
                else:
                    body = raw
            else:
                body = raw
        else:
            # 无 SKILL.md：合成最小正文
            body = f"# {display_name or name}\n\n{description}\n"
        new_skill_md = _frontmatter(name, description) + body.lstrip("\n")
        for info in zin.infolist():
            if info.is_dir():
                continue
            path = info.filename
            if path in ("SKILL.md", "skill.md"):
                continue
            zout.writestr(path, zin.read(info))
        zout.writestr("SKILL.md", new_skill_md.encode("utf-8"))
    return out.getvalue()


def _files_manifest(archive_bytes: bytes) -> list[dict]:
    files = []
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            data = z.read(info)
            files.append(
                {
                    "path": info.filename,
                    "size": len(data),
                    "hash": hashlib.sha256(data).hexdigest(),
                    "mime_type": mimetypes.guess_type(info.filename)[0] or "application/octet-stream",
                }
            )
    files.sort(key=lambda f: f["path"])
    return files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill", action="append", required=True, help="cloud skill id（可多次）")
    parser.add_argument("--agent", default=DEFAULT_AGENT_ID, help="绑定的 agent id（默认综合）")
    args = parser.parse_args()

    # 延迟导入：必须在 create_app 之前不改 sys.path（-w /app/api 已就位）
    from app_factory import create_app
    from core.db.session_factory import session_factory
    from core.tools.tool_file_manager import ToolFileManager
    from models.agent import Agent
    from models.skill import AgentSkillBinding, AgentSkillBindingSnapshot, Skill, SkillVersion
    from services.agent.skill_package_service import SkillPackageService

    flask_app = create_app()[1]  # (socketio WSGI 包装, DifyApp/Flask)
    results = []
    with flask_app.app_context():
        with session_factory.create_session() as session:
            agent = session.scalar(select(Agent).where(Agent.id == args.agent))
            if agent is None:
                print(json.dumps({"error": f"agent_not_found: {args.agent}"}), flush=True)
                os._exit(1)
            tenant_id = str(agent.tenant_id)
            user_id = str(agent.created_by) if agent.created_by else None
            active_snapshot_id = str(agent.active_config_snapshot_id) if agent.active_config_snapshot_id else None

            manifest_by_id = _load_manifest()
            zip_index = _zip_index()
            package_service = SkillPackageService()
            tool_files = ToolFileManager()

            for skill_id in args.skill:
                try:
                    entry = manifest_by_id.get(skill_id, {})
                    description = (entry.get("description") or "").strip() or f"Cloud skill {skill_id}"
                    display_name = (entry.get("name") or skill_id)[:128]
                    zip_path = zip_index.get(skill_id)
                    if not zip_path:
                        raise RuntimeError("zip_not_found")

                    name = skill_id
                    if len(name) > 64:
                        # 超长 id：先去掉常见后缀，再按连字符截断
                        for suffix in ("-full-clawhub", "-clawhub", "-skill"):
                            if name.endswith(suffix) and len(name) - len(suffix) <= 64:
                                name = name[: -len(suffix)]
                                break
                        else:
                            name = name[:64].rstrip("-")

                    # 已导入过：只补绑定
                    existing = session.scalar(
                        select(Skill).where(Skill.tenant_id == tenant_id, Skill.name == name)
                    )
                    if existing is not None and existing.latest_published_version_id is not None:
                        _ensure_bindings(
                            session, tenant_id, agent.id, active_snapshot_id, existing.id, user_id,
                            _next_priority(session, tenant_id, agent.id, active_snapshot_id),
                        )
                        session.commit()
                        results.append({"skill": skill_id, "status": "already_imported"})
                        continue

                    with open(zip_path, "rb") as f:
                        raw = f.read()
                    normalized_input = _normalize_zip(raw, name, description, display_name)
                    package = package_service.validate_and_normalize(
                        content=normalized_input, filename=f"{name}.zip"
                    )
                    archive_bytes = package.archive_bytes

                    tool_file = tool_files.create_file_by_raw(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        conversation_id=None,
                        file_binary=archive_bytes,
                        mimetype="application/zip",
                        filename=f"{name}.zip",
                    )

                    skill = Skill(
                        tenant_id=tenant_id,
                        name=name,
                        display_name=display_name,
                        icon=entry.get("icon") or "📄",
                        description=description[:1024],
                        name_manually_edited=True,
                        created_by=user_id,
                        updated_by=user_id,
                    )
                    session.add(skill)
                    session.flush()

                    sv_manifest = {
                        "name": name,
                        "display_name": display_name,
                        "description": description[:1024],
                        "files": _files_manifest(archive_bytes),
                    }
                    version = SkillVersion(
                        skill_id=skill.id,
                        version_number=1,
                        version_name="cloud-skills",
                        publish_note="cloud-skills import",
                        manifest=json.dumps(sv_manifest, ensure_ascii=False),
                        archive_tool_file_id=tool_file.id,
                        hash_code=hashlib.sha256(archive_bytes).hexdigest(),
                        archive_size=len(archive_bytes),
                        published_by=user_id,
                    )
                    session.add(version)
                    session.flush()
                    skill.latest_published_version_id = version.id

                    _ensure_bindings(
                        session, tenant_id, agent.id, active_snapshot_id, skill.id, user_id,
                        _next_priority(session, tenant_id, agent.id, active_snapshot_id),
                    )
                    session.commit()
                    results.append({"skill": skill_id, "status": "imported", "name": name})
                except Exception as exc:  # noqa: BLE001
                    session.rollback()
                    results.append({"skill": skill_id, "status": "error", "detail": str(exc)[:300]})

    print(json.dumps({"results": results}, ensure_ascii=False), flush=True)
    os._exit(0)


def _next_priority(session, tenant_id, agent_id, snapshot_id) -> int:
    """binding 与 snapshot 两表的 (tenant_id, agent_id, priority) 都有唯一约束，
    新绑定按两表现有最大值 +1 递增分配。"""
    from sqlalchemy import func

    from models.skill import AgentSkillBinding, AgentSkillBindingSnapshot

    vals = [0]
    b_max = session.scalar(
        select(func.max(AgentSkillBinding.priority)).where(
            AgentSkillBinding.tenant_id == tenant_id, AgentSkillBinding.agent_id == agent_id
        )
    )
    if b_max is not None:
        vals.append(b_max)
    if snapshot_id:
        s_max = session.scalar(
            select(func.max(AgentSkillBindingSnapshot.priority)).where(
                AgentSkillBindingSnapshot.tenant_id == tenant_id,
                AgentSkillBindingSnapshot.agent_id == agent_id,
                AgentSkillBindingSnapshot.config_snapshot_id == snapshot_id,
            )
        )
        if s_max is not None:
            vals.append(s_max)
    return max(vals) + 1


def _ensure_bindings(session, tenant_id, agent_id, snapshot_id, skill_id, user_id, priority) -> None:
    from models.skill import AgentSkillBinding, AgentSkillBindingSnapshot

    draft = session.scalar(
        select(AgentSkillBinding).where(
            AgentSkillBinding.tenant_id == tenant_id,
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.skill_id == skill_id,
        )
    )
    if draft is None:
        session.add(
            AgentSkillBinding(
                tenant_id=tenant_id,
                agent_id=agent_id,
                skill_id=skill_id,
                priority=priority,
                created_by=user_id,
            )
        )
    if snapshot_id:
        snap = session.scalar(
            select(AgentSkillBindingSnapshot).where(
                AgentSkillBindingSnapshot.tenant_id == tenant_id,
                AgentSkillBindingSnapshot.agent_id == agent_id,
                AgentSkillBindingSnapshot.config_snapshot_id == snapshot_id,
                AgentSkillBindingSnapshot.skill_id == skill_id,
            )
        )
        if snap is None:
            session.add(
                AgentSkillBindingSnapshot(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    config_snapshot_id=snapshot_id,
                    skill_id=skill_id,
                    priority=priority,
                    created_by=user_id,
                )
            )


if __name__ == "__main__":
    main()
