"""Skill／prompt 版本指紋：把每次 Agent 呼叫實際指向的 Skill 檔內容雜湊與指令雜湊寫進執行紀錄。

只新增執行紀錄（``agent_calls``、``PipelineResult.skill_versions``）的欄位，不進任何 Decision artifact、
manifest 或內容雜湊，所以舊 run 與舊驗證不受影響；事後可用它對照「某日決策是哪一版 Skill／指令產生的」。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from etf_agent.core import canonical_sha256

# SKILL.md 內連結的參考文件：Agent 被要求「及其中連結的 references/...」時會一起讀。
_REFERENCE_LINK = re.compile(r"\]\((references/[^)#\s]+\.md)")
RUN_DIR_PLACEHOLDER = "<run_dir>"
ROOT_PLACEHOLDER = "<root>"


def _file_sha256(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skill_paths_in_prompt(root: Path, prompt: str) -> List[Path]:
    """prompt 中指向專案 ``tradeagent/`` 的 ``.md`` 絕對路徑，依出現順序去重。

    每個子 Agent 的提示詞都以絕對路徑指名要先讀的 SKILL.md（及選配的 references），
    這裡以提示詞為唯一真相，不另維護一份對照表。"""
    pattern = re.compile(re.escape(str(root / "tradeagent")) + r"/\S+?\.md")
    found: List[Path] = []
    for match in pattern.finditer(prompt):
        path = Path(match.group(0))
        if path not in found:
            found.append(path)
    return found


def linked_references(skill_path: Path) -> List[Path]:
    """SKILL.md 內以 Markdown 連結指向的 ``references/*.md``（依出現順序去重；不存在的也列出，由雜湊為 null 呈現）。"""
    if not skill_path.is_file():
        return []
    text = skill_path.read_text(encoding="utf-8")
    links: List[Path] = []
    for match in _REFERENCE_LINK.finditer(text):
        path = skill_path.parent / match.group(1)
        if path not in links:
            links.append(path)
    return links


def skill_digest(root: Path, prompt: str) -> Dict[str, object]:
    """提示詞指名的 Skill 檔（含其連結的 references）逐檔 sha256，以及整組的合併雜湊。

    回傳 ``{"skill_sha256": <合併雜湊或 None>, "skill_files": {相對路徑: sha256 或 None}}``；
    提示詞沒有指向任何 Skill 檔時 ``skill_sha256`` 為 None、``skill_files`` 為空。"""
    files: Dict[str, Optional[str]] = {}
    for skill_path in skill_paths_in_prompt(root, prompt):
        for path in (skill_path, *linked_references(skill_path)):
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = path.as_posix()
            if relative not in files:
                files[relative] = _file_sha256(path)
    return {"skill_sha256": canonical_sha256(files) if files else None, "skill_files": files}


def prompt_sha256(prompt: str, replacements: Mapping[str, str]) -> str:
    """指令字串的 sha256；先把 run 目錄與專案根目錄等每次不同的絕對路徑換成固定佔位符，
    同一版指令在不同 run 才會得到相同雜湊。較長的路徑先替換（run 目錄通常在專案根目錄之下）。"""
    text = prompt
    for raw, placeholder in sorted(replacements.items(), key=lambda item: -len(item[0])):
        if raw:
            text = text.replace(raw, placeholder)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def skill_versions(calls: Iterable[Mapping[str, object]]) -> Dict[str, object]:
    """把整個 run 的 ``agent_calls`` 內逐檔 Skill 雜湊彙整成一份：``files`` 為相對路徑→sha256（取最後一次），
    ``sha256`` 為其合併雜湊；同一檔在 run 中出現不同雜湊（執行中被改動）時列在 ``inconsistent``。"""
    files: Dict[str, Optional[str]] = {}
    seen: Dict[str, set] = {}
    for call in calls:
        skill_files = call.get("skill_files")
        if not isinstance(skill_files, Mapping):
            continue
        for relative, digest in skill_files.items():
            files[str(relative)] = digest if isinstance(digest, str) else None
            seen.setdefault(str(relative), set()).add(digest)
    result: Dict[str, object] = {"files": files, "sha256": canonical_sha256(files) if files else None}
    inconsistent: Sequence[str] = sorted(path for path, digests in seen.items() if len(digests) > 1)
    if inconsistent:
        result["inconsistent"] = list(inconsistent)
    return result
