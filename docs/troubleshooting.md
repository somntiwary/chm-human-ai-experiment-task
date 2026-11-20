# Troubleshooting and operations

Run commands from the repository root. If startup used a project name such as `-p chm-review-verify`, include that same option in subsequent Compose commands.

## Startup and downloads

Inspect service state and recent output.

```sh
docker compose ps -a
docker compose logs --tail 100 frontend backend ollama-init ollama
```

A failure at `load metadata` or during an image pull occurs before application startup. Check Docker Desktop's network/proxy settings and access to the registry named in the error. The Node, Python, and PostgreSQL images use `public.ecr.aws`. The Ollama image uses Docker Hub, and model downloads use the Ollama registry and its download hosts. Retry the same startup command after restoring connectivity.

The model initializer makes at most five pull attempts with 5, 10, 20, and 40-second delays. Each attempt allows up to 30 minutes. Partial downloads are retained by Ollama. After download or cache reuse, a generation check must succeed before `Ready: qwen3:1.7b`.

The frontend then checks its page, proxied backend/database health, and a reply from the configured model before printing `Application ready`. Its check stops after three minutes and reports a diagnostic on failure. This announcement verifies startup, while later provider availability can change.

If Ollama reports insufficient memory, free memory and rerun startup. The application uses a local model and its available resources.

## Windows Ollama

Use `ollama list` on Windows to confirm the existing `llama3:latest` model. Both the backend and frontend startup check reach it through `http://host.docker.internal:11434`.

The Windows override reuses a host application; Docker does not launch that Windows application. An installed model or a leftover `ollama runner` process alone is not a running API server. A connection error at `/api/tags` means the backend cannot reach the server, before any discussion starts.

The simplest laptop start is:

```powershell
.\start-demo.ps1
```

[http://127.0.0.1:3000/](http://127.0.0.1:3000/)

The helper reuses a running server or starts the installed `ollama serve` in the background on Windows localhost. It leaves that server running for subsequent demo starts, checks the configured installed model without pulling anything, and runs the existing Compose override. `-Detached` starts Compose in the background. Docker Desktop still needs to be running. Custom remote Ollama URLs must be started separately.

When using the direct Docker command instead, open Windows Ollama first. FastAPI retries temporary connection failures for a bounded startup period and logs its target and attempt. A missing model or failed warmup has its own error. The Windows-only healthcheck allows startup/warmup time before marking the backend unhealthy. Wait for `Windows Ollama ready model=llama3:latest`, then the frontend's `Application ready` and URL.

Docker Desktop's [host networking documentation](https://docs.docker.com/desktop/features/networking/#i-want-to-connect-from-a-container-to-a-service-on-the-host) describes `host.docker.internal`; Ollama's [Windows configuration instructions](https://docs.ollama.com/faq#setting-environment-variables-on-windows) describe its separate host process. This laptop was verified with a loopback-only listener; no firewall or system-wide environment change is needed for that setup.

The [environment example](../.env.example) documents optional settings. A local `.env` can override them.

```dotenv
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=llama3:latest
```

Rerun the Windows startup command after changing environment variables. After editing only `config.yml`, reload backend configuration with the matching override.

```powershell
docker compose -f compose.yml -f compose.ollama.yml restart backend
```

After building once, a cached start can reuse existing images.

```powershell
docker compose -f compose.yml -f compose.ollama.yml up --no-build --pull never
```

[http://127.0.0.1:3000/](http://127.0.0.1:3000/)

For a background start, add `-d` and view the announcement with `docker compose -f compose.yml -f compose.ollama.yml logs frontend`.

Stop the Windows mode with the same Compose files before switching back to the reviewer command.

```powershell
docker compose -f compose.yml -f compose.ollama.yml down
```

## Seats and reconnection

Refresh preserves the current tab's `sessionStorage` token. Duplicated or restored tabs may reuse it. Open the URL independently, or use a separate browser profile, to claim another seat.

If an unfinished session is full, close its participant tabs and allow the 15-second reconnect grace to expire. Completed results remain retrievable with their existing tokens. Fresh joins create the next group. Configuration changes apply to newly created sessions.

## Verify live AI actions

```sh
docker compose logs backend
```

For the Windows mode, include both Compose files in that command. An `AI discussion` entry records the session, round, player slot, triggering message ID, remaining seconds, publication status, provider, model, reason, and elapsed milliseconds.

- `status=posted source=model provider=ollama` identifies published model chat.
- `action=vote source=model` identifies a model-generated ballot.
- `action=vote source=fallback` identifies a random fallback ballot.
- `source=none`, `status=skipped`, or `status=discarded` needs its reason checked. A generated response can be withheld before publication.

Correlate public chat IDs and text with the trigger diagnostics. Logs keep ballot choices private.

## Data and developer checks

`docker compose down` retains `postgres_data` and `ollama_models`. Keep those volumes to preserve sessions and cached models.

The backend unit tests use mocked providers and database operations or controlled ballots. Frontend tests mock readiness responses. Manual verification should additionally exercise all participant compositions, both discussions and votes, break readiness, refresh, a disconnected group, and retrieval of a completed result. Report which checks actually ran when submitting.
