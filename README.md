# Theo

Autonomous personal agent with persistent episodic and semantic memory.
Reasons, remembers, and acts through external interfaces — built for decades of continuous use.
Local-first on Apple Silicon. Async Python. Minimal dependencies, full observability.

## Quick start

Prerequisites: [just](https://github.com/casey/just) (`brew install just`), [Docker](https://www.docker.com/products/docker-desktop/), [uv](https://docs.astral.sh/uv/).

```bash
just dev                      # start infra + dashboards + agent (one command)
```

Or step by step:

```bash
just up                       # start PostgreSQL + OpenObserve
just dashboards               # provision OpenObserve dashboards + alerts
just run                      # start the agent
just down                     # stop containers
```

- **OpenObserve UI**: http://localhost:5080 (theo@theo.dev / theo)
- **PostgreSQL**: localhost:5432 (theo/theo/theo)

## Contributing

See [CLAUDE.md](CLAUDE.md) for architecture details, coding conventions, and quality gates.
