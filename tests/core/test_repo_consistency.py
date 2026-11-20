"""全專案一致性的確定性驗證：文件、設定、程式、Skill 之間互相對得上。

不連網、不讀 artifacts／.env，只讀已追蹤的檔案。每個檢查獨立，失敗訊息列出全部違規項。
已知且接受的例外寫在各檢查的 KNOWN_* 常數，附原因；新增例外前先確認不是真的缺口。
"""

import ast
import json
import re
import unittest
from pathlib import Path
from typing import Dict, Iterable, List, Set

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "etf_agent"
SKIP_DIRS = {".venv", "graphify-out", "artifacts", "var", "data", "__pycache__", ".git", "node_modules"}


def _walk(base: Path, pattern: str) -> Iterable[Path]:
    for path in sorted(base.rglob(pattern)):
        if not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            yield path


def _py_files() -> List[Path]:
    return list(_walk(SRC, "*.py"))


def _imports(path: Path) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    return names


# 執行期才產生、不入版控的選配檔（缺檔時程式視同沒有），文件可以提到。
RUNTIME_OPTIONAL_FILES = {"config/strategy_lessons.json"}


class MarkdownLinkTests(unittest.TestCase):
    LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)

    @staticmethod
    def _slug(title: str) -> str:
        title = re.sub(r"[`*_~]", "", title.strip().lower())
        title = re.sub(r"[^\w\- 　]", "", title, flags=re.UNICODE)
        return title.replace(" ", "-")

    def _anchors(self, path: Path) -> Set[str]:
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        return {self._slug(match) for match in self.HEADING.findall(text)}

    def test_relative_links_and_anchors_resolve(self):
        broken: List[str] = []
        for md in _walk(ROOT, "*.md"):
            text = re.sub(r"```.*?```", "", md.read_text(encoding="utf-8"), flags=re.DOTALL)
            text = re.sub(r"`[^`\n]*`", "", text)
            for target in self.LINK.findall(text):
                if re.match(r"^(https?:|mailto:|tel:|#?$)", target):
                    if not target.startswith("#") or target == "#":
                        continue
                path_part, _, anchor = target.partition("#")
                dest = md if not path_part else (md.parent / path_part).resolve()
                rel = md.relative_to(ROOT)
                if not dest.exists():
                    broken.append("%s -> %s（檔案不存在）" % (rel, target))
                elif anchor and dest.suffix == ".md" and self._slug(anchor) not in self._anchors(dest):
                    broken.append("%s -> %s（標題錨點不存在）" % (rel, target))
        self.assertEqual(broken, [], "壞掉的文件連結：\n" + "\n".join(broken))

    def test_backticked_repo_paths_exist(self):
        pattern = re.compile(r"`((?:src/etf_agent|config|docs|cli|scripts|tradeagent|tests)/[A-Za-z0-9_./\-]+\.(?:py|json|md|sh|yaml|yml))`")
        missing: List[str] = []
        for md in [ROOT / "AGENTS.md", ROOT / "README.md", *_walk(ROOT / "docs", "*.md"), *_walk(ROOT / "config", "*.md")]:
            if "plans" in md.parts or "source_audit" in md.parts:
                continue  # 計畫與稽核紀錄允許提到已改名或尚未建立的檔案
            for rel in set(pattern.findall(md.read_text(encoding="utf-8"))):
                if "*" in rel or "<" in rel:
                    continue
                if rel in RUNTIME_OPTIONAL_FILES:
                    continue
                if not (ROOT / rel).exists():
                    missing.append("%s 提到 %s 但檔案不存在" % (md.relative_to(ROOT), rel))
        self.assertEqual(missing, [], "文件提到不存在的檔案：\n" + "\n".join(missing))


class ConfigTests(unittest.TestCase):
    def test_all_config_json_parse_and_have_no_duplicate_keys(self):
        bad: List[str] = []

        def reject_dupes(pairs):
            keys = [key for key, _ in pairs]
            dupes = {key for key in keys if keys.count(key) > 1}
            if dupes:
                raise ValueError("重複鍵 %s" % sorted(dupes))
            return dict(pairs)

        for path in _walk(ROOT / "config", "*.json"):
            try:
                json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_dupes)
            except ValueError as exc:
                bad.append("%s：%s" % (path.relative_to(ROOT), exc))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_policy_gates_are_documented_in_status_md(self):
        policy = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        status = (ROOT / "docs" / "status.md").read_text(encoding="utf-8")
        sizing = policy.get("position_sizing", {})
        missing = [key for key in sorted(sizing) if "`%s`" % key not in status and key not in status]
        self.assertEqual(missing, [], "decision_policy.json 開著、但 docs/status.md 沒提到的閘門：%s" % missing)

    def test_status_md_policy_id_matches_config(self):
        policy = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        status = (ROOT / "docs" / "status.md").read_text(encoding="utf-8")
        policy_id = policy.get("policy_id")
        self.assertTrue(policy_id)
        self.assertIn(policy_id, status, "docs/status.md 的核對基準 policy_id 與 config 不一致：%s" % policy_id)
        rules = json.loads((ROOT / "config" / "decision_rules.json").read_text(encoding="utf-8"))
        rules_id = rules.get("rules_id") or rules.get("version") or rules.get("rules_version")
        if rules_id:
            self.assertIn(str(rules_id), status, "docs/status.md 沒寫現行 decision_rules 版本：%s" % rules_id)

    def test_gate_fields_in_config_are_known_to_sizing_validator(self):
        from etf_agent.decision.sizing import POSITION_SIZING_FIELDS

        policy = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        unknown = sorted(set(policy.get("position_sizing", {})) - set(POSITION_SIZING_FIELDS))
        self.assertEqual(unknown, [], "config 出現 validator 不認得的 position_sizing 欄位：%s" % unknown)

    def test_policy_versions_templates_parse_as_policy_shape(self):
        for path in _walk(ROOT / "config" / "policy_versions", "*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict, path.name)
            self.assertIn("position_sizing", data, "%s 缺 position_sizing，不能當回滾樣板" % path.name)


class SkillTests(unittest.TestCase):
    NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    def _skills(self) -> List[Path]:
        return [path.parent for path in _walk(ROOT / "tradeagent", "SKILL.md")]

    def test_frontmatter_name_matches_directory_and_is_kebab_case(self):
        errors: List[str] = []
        for skill in self._skills():
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            front = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
            if not front:
                errors.append("%s 缺 frontmatter" % skill.name)
                continue
            name = re.search(r"^name:\s*(\S+)", front.group(1), re.MULTILINE)
            description = re.search(r"^description:\s*(.+)", front.group(1), re.MULTILINE)
            if not name or name.group(1) != skill.name:
                errors.append("%s：name 與資料夾不符（%s）" % (skill.name, name.group(1) if name else None))
            if not self.NAME.match(skill.name):
                errors.append("%s：不是小寫連字號命名" % skill.name)
            if not description or len(description.group(1).strip()) < 20:
                errors.append("%s：description 缺漏或過短" % skill.name)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_openai_yaml_prompt_mentions_skill_name(self):
        errors: List[str] = []
        for skill in self._skills():
            yaml_path = skill / "agents" / "openai.yaml"
            if not yaml_path.exists():
                errors.append("%s 缺 agents/openai.yaml" % skill.name)
                continue
            prompt = re.search(r"default_prompt:\s*(.+)", yaml_path.read_text(encoding="utf-8"))
            if not prompt or ("$" + skill.name) not in prompt.group(1):
                errors.append("%s：default_prompt 沒提到 $%s" % (skill.name, skill.name))
        self.assertEqual(errors, [], "\n".join(errors))

    def test_skill_references_exist(self):
        errors: List[str] = []
        for skill in self._skills():
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            for rel in set(re.findall(r"`(references/[A-Za-z0-9_./\-]+)`", text)):
                if not (skill / rel).exists():
                    errors.append("%s 提到 %s 但不存在" % (skill.name, rel))
        self.assertEqual(errors, [], "\n".join(errors))


class CodeBoundaryTests(unittest.TestCase):
    def test_strategy_reflection_not_imported_by_decision_or_automation(self):
        offenders = []
        for path in _py_files():
            top = path.relative_to(SRC).parts[0]
            if top in {"decision", "automation"}:
                for name in _imports(path):
                    if "strategy_reflection" in name:
                        offenders.append("%s 匯入 %s" % (path.relative_to(ROOT), name))
        # 已知：Policy 欄位白名單的共用契約（contracts）是唯讀常數，不是行為相依
        known = {name for name in offenders if "strategy_reflection.contracts" in name}
        self.assertEqual([item for item in offenders if item not in known], [], "\n".join(offenders))

    def test_no_direct_model_api_or_agent_framework_imports(self):
        banned = ("openai", "anthropic", "langchain", "langgraph", "litellm", "google.generativeai")
        offenders = []
        for path in _py_files():
            for name in _imports(path):
                if name.split(".")[0] in banned:
                    offenders.append("%s 匯入 %s" % (path.relative_to(ROOT), name))
        self.assertEqual(offenders, [], "AGENTS.md 禁止在專案內直接串接模型 API／框架：\n" + "\n".join(offenders))

    def test_no_naive_current_time(self):
        offenders = []
        pattern = re.compile(r"datetime\.(?:utcnow|now)\(\s*\)|datetime\.today\(\)|\.utcnow\(\)")
        for path in _py_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line) and not line.lstrip().startswith("#"):
                    offenders.append("%s:%d %s" % (path.relative_to(ROOT), number, line.strip()))
        self.assertEqual(offenders, [], "出現沒帶時區的現在時間（時間點規則要求含時區）：\n" + "\n".join(offenders))

    def test_no_reimplemented_core_helpers_outside_core(self):
        names = {"canonical_json", "canonical_sha256", "content_hash", "parse_aware_datetime", "parse_finite_decimal"}
        offenders = []
        for path in _py_files():
            if "core" in path.relative_to(SRC).parts[:1]:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.FunctionDef) and node.name in names:
                    # 薄包裝（呼叫 etf_agent.core）允許；自己實作 json.dumps + hashlib 不允許
                    source = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
                    if "hashlib" in source or "json.dumps" in source:
                        offenders.append("%s:%d 自己實作 %s" % (path.relative_to(ROOT), node.lineno, node.name))
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_every_subpackage_has_init_and_is_listed_in_modules_doc(self):
        modules_doc = (ROOT / "docs" / "modules.md").read_text(encoding="utf-8")
        errors = []
        for pkg in sorted(path for path in SRC.iterdir() if path.is_dir() and path.name != "__pycache__"):
            if not (pkg / "__init__.py").exists():
                errors.append("%s 缺 __init__.py" % pkg.name)
            if pkg.name not in modules_doc:
                errors.append("docs/modules.md 沒提到子套件 %s" % pkg.name)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_cli_wrappers_do_not_contain_business_logic_size(self):
        oversized = []
        for path in _walk(ROOT / "cli", "*.py"):
            lines = len(path.read_text(encoding="utf-8").splitlines())
            if lines > 1500:
                oversized.append("%s %d 行" % (path.relative_to(ROOT), lines))
        self.assertEqual(oversized, [], "cli/ 應只是穩定包裝，過大代表邏輯可能複製進來：\n" + "\n".join(oversized))


class SecurityTests(unittest.TestCase):
    REQUIRED = [".env", ".env.*", ".envrc", "*.pem", "*.key", ".netrc", "*.p12", "*.pfx", "id_rsa*", "*.token", ".npmrc", ".pypirc", "*.jks"]

    def test_sensitive_read_globs_cover_agents_md_list(self):
        from etf_agent.automation.agent_runner import SENSITIVE_READ_GLOBS

        flat = {glob.lower() for glob in SENSITIVE_READ_GLOBS}
        missing = []
        for required in self.REQUIRED:
            if required.lower() not in flat and required.lower().lstrip("*") not in {g.lstrip("*") for g in flat}:
                missing.append(required)
        self.assertEqual(missing, [], "SENSITIVE_READ_GLOBS 缺少：%s" % missing)
        self.assertTrue(any("credential" in glob for glob in flat), "缺 credential 樣式")
        self.assertTrue(any("secret" in glob for glob in flat), "缺 secret 樣式")

    def test_no_tracked_secret_like_files(self):
        import subprocess

        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        bad = [f for f in files if re.search(r"(^|/)(\.env(\.(?!example|sample|template).*)?|.*\.pem|.*\.key|id_rsa.*|\.netrc)$", f)]
        self.assertEqual(bad, [], "已追蹤的疑似祕密檔：%s" % bad)

    def test_gitignore_covers_runtime_and_secret_paths(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for needle in (".env", "artifacts", ".venv"):
            self.assertIn(needle, text, ".gitignore 沒擋 %s" % needle)


class DependencyTests(unittest.TestCase):
    def test_every_requirement_is_pinned_in_lock_with_hash(self):
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8").lower().replace("_", "-")
        missing = []
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z0-9_.\-]+)", line.strip())
            if match and not line.startswith("#"):
                name = match.group(1).lower().replace("_", "-")
                if not re.search(r"^%s==" % re.escape(name), lock, re.MULTILINE):
                    missing.append(name)
        self.assertEqual(missing, [], "requirements.txt 有、lock 沒鎖：%s" % missing)
        self.assertIn("--hash=sha256:", lock, "requirements.lock 沒有雜湊")

    def test_third_party_imports_are_declared(self):
        declared = {
            re.match(r"^([A-Za-z0-9_.\-]+)", line).group(1).lower().replace("-", "_")
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        alias = {"yaml": "pyyaml", "yfinance": "yfinance"}
        import sys

        stdlib = set(sys.stdlib_module_names)
        local = {path.name for path in SRC.iterdir()} | {"etf_agent", "cli", "scripts", "tests", "conftest"}
        undeclared: Dict[str, str] = {}
        for path in [*_py_files(), *_walk(ROOT / "cli", "*.py"), *_walk(ROOT / "scripts", "*.py")]:
            for name in _imports(path):
                top = name.lstrip(".").split(".")[0]
                if not top or name.startswith(".") or top in stdlib or top in local:
                    continue
                if alias.get(top, top).lower().replace("-", "_") not in declared:
                    undeclared.setdefault(top, str(path.relative_to(ROOT)))
        self.assertEqual(undeclared, {}, "程式匯入但 requirements.txt 沒宣告：%s" % undeclared)


if __name__ == "__main__":
    unittest.main()
