"""Settings: defaults, then a TOML file, then a few environment variables.

Unknown keys in the TOML file are errors, so a typo cannot silently turn a guardrail off.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

GATEWAY_URL = "http://127.0.0.1:8787"


@dataclass
class LLMConfig:
    provider: str = "anthropic"            # anthropic | openai | gemini
    model: str = "claude-haiku-4-5-20251001"
    triage_model: Optional[str] = None     # defaults to model
    # The local RAG gateway during development. Set to "" to use the provider's real API with a real key.
    base_url: Optional[str] = GATEWAY_URL
    api_key_env: str = "FIXGATE_LLM_API_KEY"
    gateway_key: str = "local-test-key"    # sent when base_url points at the gateway and no key is set
    openai_api: str = "chat"               # chat | responses
    max_output_tokens: int = 8192
    thinking: str = "off"                  # off | adaptive | a token budget such as "4000"
    effort: Optional[str] = None           # low | medium | high | xhigh | max (Anthropic output_config.effort)
    request_timeout_s: float = 600.0
    prompt_caching: Optional[bool] = None  # None: on for the real Anthropic API, off through the gateway
    # How earlier tool calls are sent back: "native" tool blocks, or "prose" text. The gateway flattens native
    # tool calls into <tool_call ...>{json}</tool_call> text, and models imitating that format inside their
    # next tool call lose its arguments; prose history avoids it. "auto": prose through the gateway only.
    history_format: str = "auto"

    @property
    def uses_gateway(self) -> bool:
        return bool(self.base_url) and self.base_url.rstrip("/").startswith(("http://127.0.0.1", "http://localhost"))

    @property
    def prose_history(self) -> bool:
        return self.history_format == "prose" or (self.history_format == "auto" and self.uses_gateway)

    def api_key(self) -> Optional[str]:
        key = os.environ.get(self.api_key_env)
        if key:
            return key
        return self.gateway_key if self.uses_gateway else None


@dataclass
class RetryConfig:
    max_attempts: int = 6
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    # A subscription usage window can take hours to reset. Wait for it rather than failing the task.
    rate_limit_max_wait_s: float = 6 * 3600


@dataclass
class BudgetConfig:
    """Per task. Any limit that is reached stops the task with budget_exceeded."""

    max_cost_usd: float = 2.00
    max_steps: int = 80
    max_input_tokens: int = 4_000_000
    max_output_tokens: int = 150_000
    max_wall_s: float = 45 * 60
    warn_fraction: float = 0.8   # tell the agent to wrap up once this share of any limit is used
    # Which dollar figure max_cost_usd applies to. "measured": the usage the endpoint reports. "cached_estimate":
    # the cost on the real API with prompt caching (through the gateway, Claude Code writes the whole prompt
    # to its cache on every call, so the measured figure is several times what production pays).
    # "auto": cached_estimate through the gateway, measured otherwise.
    cost_basis: str = "auto"


@dataclass
class SandboxConfig:
    image: str = "python:3.12"          # live mode default; needs bash, git and coreutils
    workdir: str = "/workspace"
    env_setup: str = ""                  # shell prefix run before every command, e.g. activating a conda env
    memory: str = "3g"
    cpus: float = 2.0
    pids_limit: int = 1024
    exec_timeout_s: int = 180
    max_exec_timeout_s: int = 900
    output_capture_bytes: int = 2_000_000
    file_size_limit_bytes: int = 2 * 1024 ** 3
    run_as_host_user: bool = True        # live mode: files in the mounted checkout stay owned by you


@dataclass
class AgentConfig:
    tool_output_chars: int = 12_000
    context_clear_tokens: int = 60_000   # clear old tool results once the prompt grows past this estimate
    keep_recent_results: int = 8
    max_fix_rounds: int = 3              # submissions that fail fixgate's own checks before the task fails
    max_repro_rounds: int = 3
    max_blocked_commands: int = 6
    max_invalid_calls: int = 6
    max_idle_turns: int = 3              # replies without a tool call in a row
    stuck_repeat_limit: int = 4          # identical tool calls in a row
    require_reproduction: bool = False   # True: stop when the bug cannot be reproduced


@dataclass
class PolicyConfig:
    max_files_changed: int = 10
    max_lines_changed: int = 500
    forbidden_paths: List[str] = field(default_factory=lambda: [
        ".github/**", ".git/**", "**/.env", "**/*.pem", "**/*.key", "**/id_rsa*", ".gitlab-ci.yml",
    ])
    test_path_globs: List[str] = field(default_factory=lambda: [
        "tests/**", "test/**", "testing/**", "**/tests/**", "**/test/**", "**/test_*.py", "**/*_test.py",
        "**/conftest.py",
    ])
    block_test_tampering: bool = True


@dataclass
class ApprovalConfig:
    expires_hours: float = 72.0
    # Slack-compatible incoming webhook (JSON {"text": ...}) told about every new approval request.
    notify_webhook: str = ""
    page_url: str = "http://127.0.0.1:8790"   # where `fixgate serve` runs, for links in notifications


@dataclass
class GitHubConfig:
    token_env: str = "GITHUB_TOKEN"
    use_gh_cli: bool = True              # fall back to `gh auth token`
    api_url: str = "https://api.github.com"
    push_to: str = "fork"                # fork | origin
    draft: bool = True
    branch_prefix: str = "fixgate/"
    commit_name: Optional[str] = None    # defaults to your git config
    commit_email: Optional[str] = None
    watch_label: str = "fixgate"


@dataclass
class RepoProfile:
    """How to build and test one repository in live mode."""

    image: Optional[str] = None
    setup: List[str] = field(default_factory=list)   # run with network, before the agent starts
    env_setup: str = ""
    test_hint: str = ""


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(os.environ.get("FIXGATE_HOME", "~/.fixgate")).expanduser())
    llm: LLMConfig = field(default_factory=LLMConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    repos: Dict[str, RepoProfile] = field(default_factory=dict)
    pricing: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.home / "fixgate.db"

    @property
    def traces_dir(self) -> Path:
        return self.home / "traces"

    @property
    def workspaces_dir(self) -> Path:
        return self.home / "workspaces"

    def repo_profile(self, repo: str) -> RepoProfile:
        return self.repos.get(repo) or self.repos.get(repo.lower()) or RepoProfile()


class ConfigError(ValueError):
    pass


def _coerce(value: Any, current: Any, where: str) -> Any:
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true or false")
        return value
    if isinstance(current, float) and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if isinstance(current, Path):
        return Path(str(value)).expanduser()
    return value


def _merge(obj: Any, data: dict, where: str) -> None:
    names = {f.name: f for f in dataclasses.fields(obj)}
    for key, value in data.items():
        if key not in names:
            raise ConfigError(f"{where}.{key}: unknown setting")
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"{where}.{key}: expected a table")
            _merge(current, value, f"{where}.{key}")
        elif key == "repos":
            repos = {}
            for repo, profile in value.items():
                item = RepoProfile()
                _merge(item, profile, f"repos.{repo}")
                repos[repo] = item
            setattr(obj, key, repos)
        else:
            setattr(obj, key, _coerce(value, current, f"{where}.{key}"))


def load_config(path: Optional[Path] = None) -> Config:
    """Defaults, then fixgate.toml (the given path, ./fixgate.toml or ~/.fixgate/fixgate.toml), then env."""
    config = Config()
    candidates = [path] if path else [Path("fixgate.toml"), config.home / "fixgate.toml"]
    for candidate in candidates:
        if candidate and candidate.is_file():
            with open(candidate, "rb") as fh:
                _merge(config, tomllib.load(fh), "config")
            break
        if path:
            raise ConfigError(f"{path}: no such file")
    env = os.environ
    if env.get("FIXGATE_PROVIDER"):
        config.llm.provider = env["FIXGATE_PROVIDER"]
    if env.get("FIXGATE_MODEL"):
        config.llm.model = env["FIXGATE_MODEL"]
    if "FIXGATE_BASE_URL" in env:
        config.llm.base_url = env["FIXGATE_BASE_URL"] or None
    if config.llm.provider not in ("anthropic", "openai", "gemini"):
        raise ConfigError(f"llm.provider: '{config.llm.provider}' is not anthropic, openai or gemini")
    if config.budget.cost_basis not in ("auto", "measured", "cached_estimate"):
        raise ConfigError("budget.cost_basis: expected auto, measured or cached_estimate")
    if config.llm.history_format not in ("auto", "native", "prose"):
        raise ConfigError("llm.history_format: expected auto, native or prose")
    if config.github.push_to not in ("fork", "origin"):
        raise ConfigError("github.push_to: expected 'fork' or 'origin'")
    return config
