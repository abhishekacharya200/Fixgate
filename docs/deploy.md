# Running fixgate as a service

fixgate needs a Docker daemon (for the sandboxes), a GitHub token and an LLM endpoint. A small Linux VM is
enough: tasks run one at a time per `watch` process, and a sandbox is limited to the memory and CPUs set in
`[sandbox]`.

## 1. Install

```bash
python -m venv /opt/fixgate/.venv
/opt/fixgate/.venv/bin/pip install "fixgate[web,tokens] @ git+https://github.com/abhishekacharya200/fixgate.git"
mkdir -p ~/.fixgate && cp fixgate.example.toml ~/.fixgate/fixgate.toml
```

Production settings in `~/.fixgate/fixgate.toml`:

```toml
[llm]
provider = "anthropic"
model = "claude-sonnet-5"
base_url = ""                         # the real API; the key comes from FIXGATE_LLM_API_KEY

[approval]
notify_webhook = "https://hooks.slack.com/services/..."   # told about every new approval request
page_url = "http://127.0.0.1:8790"    # reach it through an SSH tunnel; it never listens publicly

[repos."your-org/your-repo"]
image = "python:3.12"
setup = ["python -m venv /tmp/venv && . /tmp/venv/bin/activate && pip install -q -e '.[test]'"]
env_setup = ". /tmp/venv/bin/activate"
test_hint = "Run tests with `pytest tests/ -q`."
```

The user that runs fixgate must be allowed to use Docker. Keep the GitHub token scoped to the
repositories fixgate works on (a fine-grained token with Issues read, Contents write on your fork, and
Pull requests write).

## 2. Services

`~/.config/systemd/user/fixgate-watch.service`:

```ini
[Unit]
Description=fixgate: work on newly labelled issues

[Service]
Environment=FIXGATE_LLM_API_KEY=...
Environment=GITHUB_TOKEN=...
ExecStart=/opt/fixgate/.venv/bin/fixgate watch your-org/your-repo --label fixgate --interval 600
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/fixgate-web.service`:

```ini
[Unit]
Description=fixgate: approval page

[Service]
Environment=GITHUB_TOKEN=...
ExecStart=/opt/fixgate/.venv/bin/fixgate serve --port 8790
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now fixgate-watch fixgate-web
loginctl enable-linger "$USER"      # keep them running after you log out
```

## 3. Day to day

- Label an issue `fixgate`. Within `--interval` seconds the watcher triages it and, if it is actionable,
  reproduces and fixes it. A verified patch becomes an approval request and the webhook posts a link.
- Review on the page (`ssh -L 8790:127.0.0.1:8790 host`, then http://127.0.0.1:8790) or with
  `fixgate show <id>`. Approve to open the draft PR, or reject with a reason.
- `fixgate tasks` and the page's task list show every outcome; `fixgate trace <task>` shows where a task
  spent its steps and money.
- `fixgate sandbox-gc` removes containers left behind if the host crashed mid-task.

State lives in `~/.fixgate/`: `fixgate.db` (tasks, approvals, audit log) and `traces/`. Back up the
database if the audit trail matters to you.
