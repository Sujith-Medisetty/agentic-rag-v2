"""
Plugin system — user-installable custom tools.

Surface mirrors the relevant subset of Rust crates/plugins/src/lib.rs:
  - manifest schema fields: name, description, schema, version, permissions,
    required_permission
  - lifecycle hooks: init() at first execute, shutdown() at process exit
  - built-in tool name collision guard
  - LocalPath install (Rust supports GitUrl too — Python does not)

The Python design intentionally diverges from Rust in two ways:
  - plugins are single Python files, not directories with plugin.json
  - execution is in-process (module.run()), not subprocess
This is a Python-first ergonomic choice. The schema fields above are mirrored
so a future enforcer can be a drop-in.

Example plugin (~/.agent/plugins/my_tool.py):

    PLUGIN_SPEC = {
        "name": "my_tool",
        "description": "Does something custom",
        "schema": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        "version": "0.1.0",                     # optional
        "permissions": ["read", "execute"],     # optional, ["read"|"write"|"execute"]
        "required_permission": "workspace-write",  # optional
    }

    def run(input: str) -> str:
        return f"Custom result: {input}"

    # Optional lifecycle hooks:
    def init(): ...
    def shutdown(): ...
"""

import atexit
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path


PLUGINS_DIR = Path.home() / ".agent" / "plugins"

VALID_PERMISSIONS = {"read", "write", "execute"}
VALID_REQUIRED_PERMISSIONS = {"read-only", "workspace-write", "danger-full-access"}

# Tool names that plugins are NOT allowed to shadow.
# Mirrors Rust with_plugin_tools() collision check (tools/src/lib.rs:144-150).
BUILTIN_TOOL_NAMES = frozenset({
    "bash", "read_file", "write_file", "edit_file",
    "grep_search", "glob_search",
    "WebFetch", "WebSearch", "TodoWrite", "Sleep",
    "AskUserQuestion", "SendUserMessage", "ToolSearch",
    "git", "github",
    "TaskCreate", "TaskGet", "TaskList", "TaskStop",
    "TaskUpdate", "TaskOutput",
    "NotebookEdit",
    "EnterPlanMode", "ExitPlanMode",
    "WorkerCreate", "WorkerSendPrompt", "WorkerGet", "WorkerObserve",
    "WorkerAwaitReady", "WorkerObserveCompletion", "WorkerTerminate",
    "WorkerRestart", "WorkerResolveTrust",
    "ListMcpResources", "ReadMcpResource", "McpAuth", "RemoteTrigger", "MCP",
})


@dataclass
class Plugin:
    name:        str
    description: str
    schema:      dict
    module_path: str
    version:     str | None = None
    permissions: list[str] = field(default_factory=list)
    required_permission: str | None = None
    _module: object | None = None
    _initialized: bool = False
    _shutdown: bool = False

    def execute(self, input_dict: dict) -> str:
        module = self._ensure_loaded()

        if not self._initialized:
            init_fn = getattr(module, "init", None)
            if callable(init_fn):
                try:
                    init_fn()
                except Exception as e:
                    raise RuntimeError(f"Plugin '{self.name}' init() failed: {e}")
            self._initialized = True

        run_fn = getattr(module, "run", None)
        if not callable(run_fn):
            raise RuntimeError(f"Plugin '{self.name}' has no run() function")

        result = run_fn(**input_dict)
        return str(result) if result is not None else "(no output)"

    def shutdown(self) -> None:
        """Mirror of Rust Plugin::shutdown(). Idempotent."""
        if self._shutdown or self._module is None:
            return
        shutdown_fn = getattr(self._module, "shutdown", None)
        if callable(shutdown_fn):
            try:
                shutdown_fn()
            except Exception as e:
                print(f"[plugins] '{self.name}' shutdown() raised: {e}")
        self._shutdown = True

    def _ensure_loaded(self) -> object:
        if self._module is None:
            spec = importlib.util.spec_from_file_location(self.name, self.module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._module = module
        return self._module


class PluginRegistry:
    """Manages user-installed plugins. Loads from ~/.agent/plugins/."""

    def __init__(self):
        self._plugins: dict[str, Plugin] = {}
        atexit.register(self._shutdown_all)

    def load_all(self) -> list[str]:
        if not PLUGINS_DIR.exists():
            return []

        loaded: list[str] = []
        for py_file in PLUGINS_DIR.glob("*.py"):
            try:
                plugin = self._load_plugin(py_file)
                if plugin:
                    self._plugins[plugin.name] = plugin
                    loaded.append(plugin.name)
            except Exception as e:
                print(f"[plugins] Failed to load {py_file.name}: {e}")
        return loaded

    def _load_plugin(self, path: Path) -> Plugin | None:
        spec = importlib.util.spec_from_file_location(path.stem, str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        plugin_spec = getattr(module, "PLUGIN_SPEC", None)
        if not plugin_spec:
            return None

        name = plugin_spec["name"]
        if name in BUILTIN_TOOL_NAMES:
            raise ValueError(
                f"plugin name '{name}' collides with a built-in tool"
            )

        permissions = plugin_spec.get("permissions") or []
        for perm in permissions:
            if perm not in VALID_PERMISSIONS:
                raise ValueError(
                    f"plugin '{name}' has invalid permission '{perm}'. "
                    f"Allowed: {sorted(VALID_PERMISSIONS)}"
                )

        required_permission = plugin_spec.get("required_permission")
        if required_permission is not None and required_permission not in VALID_REQUIRED_PERMISSIONS:
            raise ValueError(
                f"plugin '{name}' has invalid required_permission "
                f"'{required_permission}'. Allowed: {sorted(VALID_REQUIRED_PERMISSIONS)}"
            )

        plugin = Plugin(
            name=name,
            description=plugin_spec.get("description", ""),
            schema=plugin_spec.get("schema", {"type": "object", "properties": {}}),
            module_path=str(path),
            version=plugin_spec.get("version"),
            permissions=list(permissions),
            required_permission=required_permission,
        )
        plugin._module = module  # already loaded; reuse it
        return plugin

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def all_plugins(self) -> list[Plugin]:
        return list(self._plugins.values())

    def install(self, source_path: str) -> str:
        src = Path(source_path).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"Plugin file not found: {source_path}")
        if src.suffix != ".py":
            raise ValueError("Plugin must be a .py file")

        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        dst = PLUGINS_DIR / src.name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        try:
            plugin = self._load_plugin(dst)
        except Exception:
            dst.unlink(missing_ok=True)
            raise
        if not plugin:
            dst.unlink(missing_ok=True)
            raise ValueError("Plugin file has no PLUGIN_SPEC — not a valid plugin")

        self._plugins[plugin.name] = plugin
        return f"Installed plugin '{plugin.name}' from {src.name}"

    def uninstall(self, name: str) -> str:
        plugin = self._plugins.get(name)
        if not plugin:
            return f"Plugin '{name}' not found"

        plugin.shutdown()
        path = Path(plugin.module_path)
        if path.exists():
            path.unlink()

        del self._plugins[name]
        return f"Uninstalled plugin '{name}'"

    def format_list(self) -> str:
        if not self._plugins:
            return (
                "No plugins installed.\n"
                f"Install by placing .py files in: {PLUGINS_DIR}\n"
                "Or use: /plugin install <path>"
            )
        lines = [f"Installed plugins ({len(self._plugins)}):"]
        for p in self._plugins.values():
            version = f" v{p.version}" if p.version else ""
            lines.append(f"  - {p.name}{version}: {p.description[:60]}")
        return "\n".join(lines)

    def _shutdown_all(self) -> None:
        for plugin in list(self._plugins.values()):
            plugin.shutdown()


# Global plugin registry.
_global_plugins: PluginRegistry | None = None


def get_plugin_registry() -> PluginRegistry:
    global _global_plugins
    if _global_plugins is None:
        _global_plugins = PluginRegistry()
        _global_plugins.load_all()
    return _global_plugins
