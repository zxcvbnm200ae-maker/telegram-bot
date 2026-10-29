"""Noor Telegram project builder: generate, validate, test, repair, and ZIP projects."""
from __future__ import annotations

import ast
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


MAX_FILES = 40
MAX_FILE_BYTES = 250_000
MAX_PROJECT_BYTES = 2_000_000
TEST_TIMEOUT_SECONDS = 25
REPAIR_ATTEMPTS = 2

SYSTEM_PROMPT = """You are Noor, a careful software engineering agent. Build complete, coherent projects from Arabic or English requirements and attached flowchart images.
Return ONLY a JSON object with this schema: {\"project_name\": \"short-name\", \"summary\": \"Arabic summary\", \"files\": [{\"path\": \"relative/path\", \"content\": \"full file contents\"}], \"test_plan\": [\"test/check description\"]}.
Rules:
- Include all source files, README.md, and relevant tests. Do not use placeholders, TODOs, ellipses, or omit essential files.
- Interpret any attached diagram as a specification; state assumptions in README if unclear.
- Prefer simple, maintainable code and explicit error handling.
- Never include real credentials, tokens, private keys, or secrets. Use environment variables and .env.example with dummy values only.
- Do not add malware, credential theft, covert surveillance, destructive actions, or code intended to harm systems.
- For Python projects, include pytest tests where practical. Tests should not need network access or real secrets.
- File paths must be relative and must not escape the project directory.
- Provide a runnable README with setup and run instructions.
"""

# Modules and calls that are too risky to auto-execute in an Android Termux process.
BLOCKED_IMPORTS = {
    "os", "subprocess", "socket", "ctypes", "multiprocessing", "pty", "resource",
    "shutil", "pathlib", "requests", "httpx", "urllib", "ftplib", "telnetlib",
    "pickle", "shelve", "importlib", "runpy", "webbrowser", "signal",
}
BLOCKED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}


def safe_relative_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\\" in raw:
        raise ValueError(f"Invalid project path: {raw!r}")
    p = PurePosixPath(raw.strip())
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        raise ValueError(f"Unsafe project path: {raw!r}")
    if p.parts and ":" in p.parts[0]:
        raise ValueError(f"Unsafe project path: {raw!r}")
    return str(p)


def parse_model_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ValueError("Model response did not contain a valid files list")
    if not data["files"] or len(data["files"]) > MAX_FILES:
        raise ValueError(f"Project must contain between 1 and {MAX_FILES} files")
    total = 0
    normalized = []
    seen = set()
    for item in data["files"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid file entry")
        rel = safe_relative_path(item.get("path", ""))
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"File content must be text: {rel}")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError(f"File too large: {rel}")
        if rel in seen:
            raise ValueError(f"Duplicate file path: {rel}")
        seen.add(rel)
        total += size
        normalized.append({"path": rel, "content": content})
    if total > MAX_PROJECT_BYTES:
        raise ValueError("Project is too large")
    data["files"] = normalized
    data["project_name"] = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(data.get("project_name") or "noor-project")).strip("-")[:48] or "noor-project"
    data["summary"] = str(data.get("summary") or "تم إنشاء المشروع.")[:1500]
    return data


def scan_python_safety(project_dir: Path) -> list[str]:
    """Conservative static scan. This is not a true OS sandbox."""
    problems: list[str] = []
    for file in project_dir.rglob("*.py"):
        try:
            source = file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file))
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(f"{file.relative_to(project_dir)}: Python syntax/encoding error: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in BLOCKED_IMPORTS:
                        problems.append(f"{file.relative_to(project_dir)}: blocked import '{root}'")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in BLOCKED_IMPORTS:
                    problems.append(f"{file.relative_to(project_dir)}: blocked import '{root}'")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_CALLS:
                    problems.append(f"{file.relative_to(project_dir)}: blocked call '{node.func.id}()'")
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"system", "popen", "spawn", "remove", "rmtree", "unlink"}:
                    problems.append(f"{file.relative_to(project_dir)}: risky call '.{node.func.attr}()'")
    return sorted(set(problems))


def _model_request(client: Any, model: str, prompt: str, image_bytes: bytes | None = None, repair_context: str = "") -> dict[str, Any]:
    user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt + ("\n\nRepair report and previous files:\n" + repair_context if repair_context else "")}]
    if image_bytes:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        user_content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded, "detail": "high"}})
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
        response_format={"type": "json_object"},
        temperature=0.15,
    )
    return parse_model_json(response.choices[0].message.content or "")


def _write_project(data: dict[str, Any], project_dir: Path) -> None:
    for item in data["files"]:
        rel = safe_relative_path(item["path"])
        destination = project_dir.joinpath(*PurePosixPath(rel).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item["content"], encoding="utf-8")


def run_checks(project_dir: Path) -> tuple[bool, str]:
    """Run static Python checks; execute pytest only after a conservative safety scan."""
    py_files = list(project_dir.rglob("*.py"))
    if not py_files:
        return True, "No Python files found; only project structure was checked. Runtime tests were not executed."

    compile_result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(project_dir)],
        cwd=str(project_dir), capture_output=True, text=True, timeout=TEST_TIMEOUT_SECONDS,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if compile_result.returncode:
        return False, "Python compile check failed:\n" + (compile_result.stderr or compile_result.stdout)[-5000:]

    tests_dir = project_dir / "tests"
    test_files = list(tests_dir.rglob("test_*.py")) if tests_dir.exists() else []
    if not test_files:
        return True, "PASS: Python syntax compilation. WARNING: no pytest test files found; behavior is not verified."

    safety = scan_python_safety(project_dir)
    if safety:
        return False, "Static safety review blocked automatic test execution:\n" + "\n".join(safety[:30]) + "\nTests were NOT executed. Revise code or review it manually."

    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "NOOR_TEST_MODE": "1"}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests"], cwd=str(project_dir),
            capture_output=True, text=True, timeout=TEST_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"pytest exceeded {TEST_TIMEOUT_SECONDS} seconds and was stopped."
    output = (result.stdout + "\n" + result.stderr).strip()[-6000:]
    if result.returncode:
        return False, "pytest FAILED:\n" + output
    return True, "PASS: Python syntax compilation. PASS: pytest.\n" + output


def make_zip(project_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(project_dir.rglob("*")):
            if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc":
                continue
            archive.write(file, file.relative_to(project_dir.parent))


def build_project(prompt: str, image_bytes: bytes | None = None) -> dict[str, Any]:
    """Generate, validate, run permitted checks, repair failures, and create a ZIP."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY غير مضبوط. أضف مفتاح API في متغير بيئة Termux أولاً.")
    model = os.getenv("NOOR_MODEL", "gpt-4.1-mini").strip()
    from openai import OpenAI
    client = OpenAI(api_key=api_key, timeout=90.0, max_retries=2)
    last_report = ""
    with tempfile.TemporaryDirectory(prefix="noor_build_") as temp:
        root = Path(temp)
        project_dir = root / "project"
        project_dir.mkdir()
        data = _model_request(client, model, prompt, image_bytes)
        for attempt in range(REPAIR_ATTEMPTS + 1):
            # Rebuild cleanly each attempt; never retain stale files from an earlier model response.
            shutil.rmtree(project_dir, ignore_errors=True)
            project_dir.mkdir(parents=True)
            _write_project(data, project_dir)
            ok, report = run_checks(project_dir)
            last_report = report
            if ok:
                zip_path = root / f"{data['project_name']}.zip"
                make_zip(project_dir, zip_path)
                return {
                    "ok": True, "name": data["project_name"], "summary": data["summary"],
                    "report": report, "zip_bytes": zip_path.read_bytes(),
                    "test_plan": data.get("test_plan", []), "attempts": attempt + 1,
                }
            if attempt >= REPAIR_ATTEMPTS:
                break
            repair_context = report + "\n\nCurrent files JSON:\n" + json.dumps(data["files"], ensure_ascii=False)[:35_000]
            data = _model_request(client, model, prompt, image_bytes, repair_context)
        return {"ok": False, "name": data.get("project_name", "noor-project"), "summary": data.get("summary", ""), "report": last_report, "attempts": REPAIR_ATTEMPTS + 1}
