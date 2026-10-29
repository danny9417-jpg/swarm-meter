"""Conservative roles for local process accounting, never activity guesses.

A process can own RAM while waiting for a tool request or a remote model. CPU
silence is therefore not an idle flag. We identify local work from executable /
script tokens, keep host and service overhead separate, and expose everything
we cannot recognize as unclassified. No files named by argv are opened here.
"""

from __future__ import annotations

from pathlib import PurePath
import re
import shlex

ROLES = ("workload", "host", "helper", "unclassified")
PYTHON = re.compile(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?$")
INTERPRETERS = {"node", "nodejs", "ruby", "perl", "lua", "julia", "rscript"}
SHELLS = {"sh", "bash", "dash", "zsh", "fish", "ksh"}
WORK_EXECUTABLES = {
    "ffmpeg",
    "ffprobe",
    "whisper",
    "whisper-cli",
    "whisper-cpp",
    "llama-cli",
    "pytest",
    "py.test",
    "ruff",
    "mypy",
    "pylint",
    "flake8",
    "black",
    "isort",
    "gcc",
    "g++",
    "cc",
    "c++",
    "clang",
    "clang++",
    "cc1",
    "cc1plus",
    "rustc",
    "ld",
    "ld.lld",
    "lld",
    "as",
    "nasm",
    "nvcc",
    "ptxas",
    "lean",
    "rg",
    "grep",
    "sed",
    "awk",
    "sort",
    "jq",
    "tar",
    "unzip",
    "gzip",
    "xz",
}
WORK_MODULES = {
    "pytest",
    "unittest",
    "compileall",
    "whisper",
    "faster_whisper",
    "torch.distributed.elastic.multiprocessing.errors",
}
WORK_SCRIPT_NAMES = {
    "dec",
    "decode",
    "decoder",
    "transcribe",
    "transcription",
    "train",
    "training",
    "infer",
    "inference",
    "evaluate",
    "eval",
    "benchmark",
    "bench",
    "analysis",
    "analyze",
    "analyse",
    "convert",
    "render",
    "cpuvox",
}
SERVICE_MODULES = {
    "mcp",
    "fastmcp",
    "uvicorn",
    "gunicorn",
    "http.server",
    "jupyter",
    "jupyterlab",
    "notebook",
    "ipykernel_launcher",
    "debugpy",
    "pylsp",
}
SERVICE_NAMES = {
    "uvicorn",
    "gunicorn",
    "jupyter",
    "jupyter-lab",
    "jupyter-notebook",
    "pylsp",
    "pyright-langserver",
    "typescript-language-server",
    "tsserver",
    "swarm_meter.py",
    "swarm-meter",
    "chrome",
    "chromium",
    "firefox",
    "electron",
    "chatgpt",
    "code",
    "code-insiders",
}
LAUNCHERS = {
    "env",
    "timeout",
    "nice",
    "nohup",
    "setsid",
    "uv",
    "uvx",
    "npx",
    "npm",
    "yarn",
    "pnpm",
    "make",
    "ninja",
    "torchrun",
    "accelerate",
}


def argv_tokens(process):
    """Live /proc argv keeps argument boundaries; synthetic fixtures use shlex."""
    argv = process.get("argv")
    if isinstance(argv, (list, tuple)) and argv:
        return [str(part) for part in argv if part is not None]
    try:
        return shlex.split(process.get("cmd", "")) or [process.get("name", "")]
    except ValueError:
        return [process.get("name", "")]


def command_identity(process):
    """Return executable and actual script/module, excluding arbitrary arguments.

    In particular `codex --prompt 'python train.py'` is just Codex. Shell -c
    strings and Python -c bodies are not reparsed as additional executables.
    """
    # Reusing parsed evidence avoids repeatedly reparsing shared ancestors. The
    # full command signature invalidates this cache across exec, even at one PID.
    raw_argv = process.get("argv")
    signature = (
        tuple(raw_argv) if isinstance(raw_argv, (list, tuple)) and raw_argv else None,
        process.get("cmd", ""),
        process.get("name", ""),
    )
    cached = process.get("_command_identity_cache")
    if cached and cached[0] == signature:
        return cached[1]
    argv = argv_tokens(process)
    executable = PurePath(argv[0]).name.lower() if argv else ""
    target, mode, remaining = "", "executable", argv[1:]
    if PYTHON.fullmatch(executable) or executable in INTERPRETERS:
        mode = "interpreter"
        i = 1
        while i < len(argv):
            item = argv[i]
            if item == "--":
                i += 1
                if i < len(argv):
                    target, mode, remaining = argv[i], "script", argv[i + 1 :]
                break
            if item in ("-c", "-e", "--eval", "-p", "--print"):
                mode, remaining = "inline", []
                break
            if item == "-m" and PYTHON.fullmatch(executable):
                if i + 1 < len(argv):
                    target, mode, remaining = argv[i + 1], "module", argv[i + 2 :]
                break
            if item in ("-W", "-X", "--require", "-r", "--loader", "--import"):
                i += 2
                continue
            if item.startswith("-"):
                i += 1
                continue
            target, mode, remaining = item, "script", argv[i + 1 :]
            break
    result = {
        "executable": executable,
        "target": target,
        "mode": mode,
        "arguments": remaining,
        "argv": argv,
    }
    process["_command_identity_cache"] = (signature, result)
    return result


def agent_host_kind(process):
    # Host recognition depends only on this exact parsed command. Keep it beside
    # intrinsic roles rather than rebuilding package paths for every ancestor.
    identity = command_identity(process)
    if "host_kind" not in identity:
        identity["host_kind"] = _agent_host_kind_uncached(process)
    return identity["host_kind"]


def _agent_host_kind_uncached(process):
    """Match actual agent entry points, never arbitrary project folder names."""
    identity = command_identity(process)
    executable = identity["executable"]
    native = {
        "claude": "Claude Code",
        "codex": "Codex",
        "gemini": "Gemini CLI",
        "openclaw": "OpenClaw",
        "openclaw-gateway": "OpenClaw",
        "ollama": "Ollama",
        "ollama_llama_server": "Ollama",
    }
    if executable in native:
        return native[executable]
    if executable not in {"node", "nodejs"} or identity["mode"] != "script":
        return None
    parts = PurePath(identity["target"]).parts
    # npm package entrypoints are explicit launch evidence. Merely having a
    # project directory called codex/openclaw/claude-code is not sufficient.
    for index, part in enumerate(parts):
        if part != "node_modules":
            continue
        package_path = parts[index + 1 :]
        entries = {
            ("@openai", "codex", "bin", "codex.js"): "Codex",
            ("@anthropic-ai", "claude-code", "cli.js"): "Claude Code",
            ("@google", "gemini-cli", "dist", "index.js"): "Gemini CLI",
            ("openclaw",): "OpenClaw",
            ("openclaw", "openclaw.mjs"): "OpenClaw",
            ("openclaw", "dist", "index.js"): "OpenClaw",
        }
        if package_path in entries:
            return entries[package_path]
    return None


def _mcp_marker(identity):
    """Recognize service entry points, not arbitrary directories named mcp."""
    target = identity["target"] or identity["executable"]
    base = PurePath(target).name.lower()
    module = target.lower() if identity["mode"] == "module" else ""

    def marker(part):
        return part in {
            "mcp",
            "fastmcp",
            "mcp-server",
            "mcp_server",
        } or part.startswith(("mcp-server-", "mcp_server_"))

    if module and marker(module.split(".")[0]):
        return True
    if marker(PurePath(base).stem):
        return True
    parts = list(PurePath(target.lower()).parts)
    # Installed service package paths are evidence; /project/mcp/decode.py is not.
    for package_root in ("node_modules", "site-packages", "dist-packages"):
        if package_root in parts:
            following = parts[parts.index(package_root) + 1 :]
            if (
                following[:1] == ["@modelcontextprotocol"]
                and len(following) > 2
                and following[1].startswith("server-")
            ):
                return True
            if any(marker(part) for part in following[:-1]):
                return True
    return base == "decomp" and identity["arguments"][:1] == ["mcp"]


def _intrinsic(process):
    # The cache belongs to this exact argv identity, not just a numeric PID.
    identity = command_identity(process)
    if "intrinsic_role" not in identity:
        identity["intrinsic_role"] = _intrinsic_uncached(process)
    return identity["intrinsic_role"]


def _intrinsic_uncached(process):
    """Certain roles determined from this process alone; None means uncertain."""
    identity = command_identity(process)
    exe, target, mode, args = (
        identity[k] for k in ("executable", "target", "mode", "arguments")
    )
    script = PurePath(target).name.lower()
    if _mcp_marker(identity):
        return "helper", "Known MCP connector/service executable or module"
    # This installed CUA tool runtime remains resident between requests. Its
    # process is service overhead; no local-process signal exposes its tasks.
    package_parts = PurePath(target).parts
    if exe in {"node", "nodejs"} and mode == "script" and script == "cua-repl.mjs":
        if any(
            package_parts[i : i + 3] == ("node_modules", "@oai", "cua-repl")
            for i in range(len(package_parts) - 2)
        ):
            return (
                "helper",
                "Resident Codex CUA REPL service; internal task activity is unobservable",
            )
    if (
        exe in SERVICE_NAMES
        or script in SERVICE_NAMES
        or (
            mode == "module"
            and any(target == m or target.startswith(m + ".") for m in SERVICE_MODULES)
        )
    ):
        return "helper", "Known desktop, language-server, monitoring or API service"
    # A known monitor's private Lean protocol helper is service overhead, not a build.
    if exe == "core" and any(
        p in str(identity["argv"][0])
        for p in ("agent-meter/", "swarm-meter/", "astra-ui-redesign/")
    ):
        return "helper", "Swarm Meter's private Lean decision helper"
    if exe in SHELLS:
        return "helper", "Shell/controller overhead; child work is counted separately"
    if exe in LAUNCHERS:
        return "helper", "Launch/build controller; child work is counted separately"
    if exe == "cmake" and "--build" in args:
        return "helper", "Build controller; compiler child work is counted separately"
    if mode == "module" and target in {
        "torch.distributed.run",
        "torch.distributed.launch",
        "accelerate.commands.launch",
        "vllm.entrypoints.openai.api_server",
    }:
        return (
            "helper",
            "Training/inference launcher or API service; worker processes are separate",
        )
    if exe in {"godot", "godot4", "godot-mono"}:
        if any(
            a
            in {"--script", "-s", "--export-release", "--export-debug", "--export-pack"}
            for a in args
        ):
            return "workload", "Explicit Godot script/export operation"
        return (
            "helper",
            "Persistent editor/application; no script/export workload identified",
        )
    if exe in {"ollama", "ollama_llama_server", "llama-server"}:
        if exe in {"ollama_llama_server", "llama-server"} or (
            exe == "ollama" and args[:1] == ["runner"]
        ):
            return (
                "workload",
                "Local model runner; process presence does not prove an active request",
            )
        if exe == "ollama" and (not args or args[:1] == ["serve"]):
            return "host", "Ollama host server"
        return "helper", "Model client/service; active local inference is unconfirmed"
    if exe in WORK_EXECUTABLES or exe == "cmake":
        return "workload", "Recognized local tool/compiler operation"
    if mode == "module" and (target in WORK_MODULES or target.startswith("whisper.")):
        return "workload", "Recognized local worker/test module"
    if mode == "script" and PurePath(script).stem in WORK_SCRIPT_NAMES:
        return "workload", "Recognized local worker script"
    return None


def classify_process(process, procs, owner_kind, host_pid):
    """Role is evidence about process purpose, never an inference from CPU silence."""
    if process.get("state") == "Z":
        return "unclassified", "Exited zombie awaiting parent reaping; not live work"
    own = _intrinsic(process)
    driver = command_identity(process)["executable"]
    if driver in {"gcc", "g++", "cc", "c++", "clang", "clang++"}:
        worker_names = {
            "cc1",
            "cc1plus",
            "clang",
            "clang++",
            "as",
            "ld",
            "ld.lld",
            "lld",
        }
        if any(
            child.get("ppid") == process["pid"]
            and child.get("state") != "Z"
            and command_identity(child)["executable"] in worker_names
            for child in procs.values()
        ):
            return (
                "helper",
                "Compiler driver; observed compiler child is counted separately",
            )
    if own:
        return own
    identity = command_identity(process)
    exe, target, mode = (identity[k] for k in ("executable", "target", "mode"))
    # Docker identifies a container's ownership, not an agent-host executable.
    # Host identity must be an actual CLI token, not an argument mentioning one.
    if (
        owner_kind != "Docker"
        and process["pid"] == host_pid
        and agent_host_kind(process) is not None
    ):
        return "host", "Agent host/controller; not an additional local workload"
    # Child interpreters of a known service are not independent jobs without
    # their own positive work evidence (handled above). Walk through wrappers.
    current = procs.get(process.get("ppid"))
    seen = set()
    while current and current["pid"] not in seen and current["pid"] != host_pid:
        seen.add(current["pid"])
        parent_role = _intrinsic(current)
        if (
            parent_role
            and parent_role[0] == "helper"
            and command_identity(current)["executable"] not in SHELLS | LAUNCHERS
        ):
            return (
                "helper",
                "Descendant of a known service; no separate workload evidence",
            )
        current = procs.get(current.get("ppid"))
    if mode == "script" and PurePath(target).suffix.lower() in {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".rb",
        ".pl",
        ".jl",
        ".r",
    }:
        if PurePath(target).stem.lower() in {
            "server",
            "app",
            "main",
            "index",
            "bootstrap",
            "bridge",
            "kernel",
            "trusted-worker",
        }:
            return (
                "unclassified",
                "Generic application/server script; workload purpose is unconfirmed",
            )
        return "workload", "Explicit local script outside a known service subtree"
    if mode == "inline":
        return (
            "unclassified",
            "Inline interpreter command; workload purpose is unconfirmed",
        )
    return (
        "unclassified",
        "No reliable local-work or service signature; RAM remains accounted",
    )
