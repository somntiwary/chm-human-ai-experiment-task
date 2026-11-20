import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, UUID4, ValidationError

from . import db
from .config import load_experiment_config
from .experiment import Action, Experiment, ParticipantConnection, SeatExpired, SessionFull
from .llm_player import OLLAMA_RUNNER_OPTIONS


logger = logging.getLogger(__name__)
startup_logger = logging.getLogger("uvicorn.error")
WINDOWS_OLLAMA_CONNECT_ATTEMPTS = 6
WINDOWS_OLLAMA_RETRY_DELAYS = (2, 4, 8, 10, 10)
actions = TypeAdapter(Action)


class JoinRequest(BaseModel):
    join_key: UUID4


class SocketAuth(BaseModel):
    token: str = Field(min_length=43, max_length=43)


async def preload_ollama(base_url: str, model: str) -> None:
    """Verify the configured model is installed and load it outside the round timer."""
    if not base_url or not model:
        raise RuntimeError("Set OLLAMA_BASE_URL and OLLAMA_MODEL for the Windows Ollama override.")

    timeout = httpx.Timeout(120, connect=5)
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False) as client:
            for attempt in range(1, WINDOWS_OLLAMA_CONNECT_ATTEMPTS + 1):
                try:
                    response = await client.get("/api/tags", timeout=5)
                    response.raise_for_status()
                except httpx.RequestError as error:
                    reason = type(error).__name__
                except httpx.HTTPStatusError as error:
                    status = error.response.status_code
                    if status not in (502, 503, 504):
                        raise RuntimeError(
                            f"Windows Ollama at {base_url} returned HTTP {status} while listing models. "
                            "Check OLLAMA_BASE_URL and Windows Ollama logs, then rerun Compose."
                        ) from None
                    reason = f"HTTP {status}"
                else:
                    break

                if attempt == WINDOWS_OLLAMA_CONNECT_ATTEMPTS:
                    raise RuntimeError(
                        f"Cannot reach Windows Ollama at {base_url} for {model} after {attempt} checks "
                        f"({reason}). Start Windows Ollama with '.\\start-demo.ps1', or verify "
                        "OLLAMA_BASE_URL is reachable from Docker and rerun Compose. "
                        "The direct Compose override does not start Ollama or download models."
                    ) from None
                delay = WINDOWS_OLLAMA_RETRY_DELAYS[attempt - 1]
                startup_logger.warning(
                    "Waiting for Windows Ollama url=%s model=%s attempt=%d/%d retry_seconds=%d reason=%s. "
                    "Start Windows Ollama on the host before the demo.",
                    base_url, model, attempt, WINDOWS_OLLAMA_CONNECT_ATTEMPTS, delay, reason,
                )
                await asyncio.sleep(delay)

            try:
                models = response.json()["models"]
                if not isinstance(models, list) or any(
                    not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in models
                ):
                    raise ValueError("invalid model list")
            except (ValueError, TypeError, KeyError):
                raise RuntimeError(
                    f"Windows Ollama at {base_url} returned an invalid model list. "
                    "Check OLLAMA_BASE_URL and Windows Ollama logs."
                ) from None
            if not any(row["name"] == model for row in models):
                raise RuntimeError(
                    f"{model} is not installed in Windows Ollama. Run 'ollama list' and set OLLAMA_MODEL "
                    "to an already installed model. This override does not download models. "
                    "Alternatively, use 'docker compose up --build' for the bundled reviewer model."
                ) from None

            startup_logger.info("Loading Windows Ollama model=%s url=%s", model, base_url)
            try:
                response = await client.post("/api/chat", json={
                    "model": model,
                    "messages": [],
                    "stream": False,
                    "keep_alive": "10m",
                    "options": OLLAMA_RUNNER_OPTIONS,
                })
                response.raise_for_status()
                ready = response.json()
                if not isinstance(ready, dict) or ready.get("model") != model or ready.get("done") is not True:
                    raise ValueError("model did not confirm loading")
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                raise RuntimeError(
                    f"Windows Ollama is reachable at {base_url}, but installed model {model} could not load. "
                    "Check Windows Ollama logs and available memory, then rerun Compose."
                ) from None
    except httpx.InvalidURL:
        raise RuntimeError(
            "OLLAMA_BASE_URL is invalid. Set it to http://host.docker.internal:11434 for Windows Ollama."
        ) from None
    startup_logger.info("Windows Ollama ready model=%s", model)


@asynccontextmanager
async def lifespan(app: FastAPI):
    default_config = Path(__file__).resolve().parents[2] / "config.yml"
    config_path = Path(os.environ.get("EXPERIMENT_CONFIG", default_config))
    config = load_experiment_config(config_path)
    database_url = os.environ["DATABASE_URL"]
    if "ai" in config.participants and os.getenv("OLLAMA_PRELOAD") == "1":
        await preload_ollama(os.getenv("OLLAMA_BASE_URL", "").strip(), os.getenv("OLLAMA_MODEL", "").strip())
    await db.initialize(database_url)

    app.state.database_url = database_url
    app.state.experiment = Experiment(database_url, config)
    clock = asyncio.create_task(app.state.experiment.run_clock())
    try:
        yield
    finally:
        clock.cancel()
        with suppress(asyncio.CancelledError):
            await clock
        await app.state.experiment.stop_ai()


app = FastAPI(title="Decision Study", lifespan=lifespan)


@app.get("/api/health")
def health(request: Request) -> JSONResponse:
    if db.database_is_ready(request.app.state.database_url):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "unavailable"}, status_code=503)


@app.post("/api/join")
async def join(body: JoinRequest, request: Request) -> JSONResponse:
    try:
        token = await request.app.state.experiment.join(body.join_key)
    except SessionFull as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except SeatExpired:
        raise HTTPException(status_code=410, detail="That seat expired. Click Join study to try again.") from None
    except psycopg.Error:
        logger.warning("Could not admit a player")
        raise HTTPException(status_code=503, detail="The study is temporarily unavailable.") from None
    return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})


async def send_updates(websocket: WebSocket, participant: ParticipantConnection) -> None:
    while True:
        await websocket.send_json(await participant.updates.get())


@app.websocket("/api/ws")
async def live_connection(websocket: WebSocket) -> None:
    await websocket.accept()
    experiment = websocket.app.state.experiment
    try:
        try:
            auth = SocketAuth.model_validate_json(
                await asyncio.wait_for(websocket.receive_text(), timeout=5)
            )
        except (TimeoutError, KeyError, ValidationError):
            await websocket.close(code=4401)
            return

        participant = await experiment.subscribe(auth.token)
        if participant is None:
            await websocket.close(code=4401)
            return

        sender = asyncio.create_task(send_updates(websocket, participant))
        try:
            while True:
                incoming = await websocket.receive()
                if incoming["type"] == "websocket.disconnect":
                    break
                try:
                    action = actions.validate_json(incoming.get("text") or "")
                except ValidationError:
                    participant.push({"type": "error", "message": "Invalid message or vote."})
                    continue
                await experiment.act(participant, action)
        finally:
            await experiment.unsubscribe(participant)
            sender.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect):
                await sender
    except WebSocketDisconnect:
        pass
    except psycopg.Error:
        logger.warning("Study connection lost access to PostgreSQL")
        with suppress(WebSocketDisconnect):
            await websocket.close(code=1011)
