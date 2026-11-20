# Agent verification — 2026-11-20

Checked with `participants: [human, ai, ai]` and the unchanged 60-second discussion timer.

## What changed

- One shared participant instruction replaces the greeting, nomination, criteria, and opening scripts. Only real public dialogue becomes model chat messages.
- Explicit addresses apply to required replies, spontaneous openings, and idle turns. General follow-ups stay with the latest speaker. Initial speaker order is randomized.
- Round 2 has a nine-second window for humans to lead. Spontaneous turns are optional; one human input enables a bounded peer exchange.
- Model output can use one to three short sentences. Optional inference can take up to 15 seconds while remaining interruptible; the server deadline still controls publication and voting.
- Self-narration, ballot announcements, echoes, and substantial near-verbatim replies are checked before publication. Failed chat stays silent instead of becoming a scripted reply.
- Private voting hides the voter's identity and uses freshly shuffled P1/P2/P3 candidate identifiers. The server validates and maps the choice back to the actual A/B/C player. Self-voting remains eligible.

## Checks actually performed

| Check | Result |
| --- | --- |
| Complete backend suite in Docker | 91 tests passed |
| All six private identifier permutations, each of three choices | Correct actual player returned in all 18 mapping checks |
| Default reviewer mode, live `qwen3:1.7b` | Both rounds, direct questions, follow-ups, peer contributions, four model ballots, final result, and result reconnection completed |
| Windows override, installed `llama3:latest` | Both rounds, direct questions, follow-ups, peer contributions, four model ballots, and final result completed |
| Exact “Hey player C, are you honest?? …” question | C replied in its own voice; other players did not answer that directed message on C's behalf |
| Different Round 2 uncertainty question | Replies addressed the new question rather than repeating the Round 1 introduction |
| Controlled label swap after blinded voting | Both models chose the stronger contribution under each of two A/B assignments in the fixture |
| Both startup commands | Frontend served on port 3000 and printed `Application ready` followed by the URL |
| Model reuse | Cached Qwen and the already installed Windows Llama model were reused; no model downloads were performed |

The full-session checks used the frontend/browser plus the application's HTTP/WebSocket actions, and correlated published messages with backend trigger IDs and `source=model` logs. Individual live ballots were not printed. The controlled label-swap ballots used synthetic contributions outside participant sessions.

Final full-run published chat calls took approximately 3.9–5.9 seconds for Qwen and 3.5–6.4 seconds for Llama on this laptop. Earlier valid calls took nearly 15 seconds, motivating the optional-call budget correction. These observations are not performance guarantees.

Startup commands were exercised with `-d --build` for background verification:

```powershell
docker compose up -d --build
docker compose -f compose.yml -f compose.ollama.yml up -d --build
```

The backend suite ran with existing container dependencies and a read-only source mount:

```powershell
docker compose run --rm --no-deps -T --volume "${PWD}:/checks:ro" --workdir /checks/backend -e PYTHONDONTWRITEBYTECODE=1 backend python -m unittest discover -s tests
```

These finite checks cannot establish zero hallucinations, universal relevance, or unbiased model behavior. Chat uses current-round evidence; prior-round discussion is deliberately unavailable. Genuine errors, optional silence, and responses arriving after the deadline can still result in no published message. Server diagnostics record the reason.

## Files changed

- `backend/app/llm_player.py`
- `backend/app/experiment.py`
- `backend/app/discussion.py`
- `backend/app/discussion_output.py`
- `backend/tests/test_llm_player.py`
- `backend/tests/test_discussion_scheduling.py`
- `backend/tests/test_discussion.py`
- `backend/tests/test_discussion_output.py`
- `compose.yml`
- `README.md`
- `docs/agent-verification.md`

For a quick manual check, open a fresh browser context, join, and ask one displayed player a direct question early in the round. Follow up after its reply. In Round 2, introduce a different concrete question. Inspect `docker compose logs backend` to correlate the reply's trigger, remaining time, actual model, and publication status.

## Round 2 public ballot announcement regression

The stored transcripts and backend diagnostics from sessions 21–23 attributed “I will proceed to vote for myself” to `source=model`, `provider=ollama`, `model=qwen3:1.7b` during discussion. It did not come from the private ballot path or a fallback. The earlier privacy expression missed intervening words such as “proceed to”; an added suffix also escaped exact duplicate detection. False claims of being ineligible were not validated. Round 2's opening instructions also foregrounded the previous vote; both rounds now explicitly identify the current public-discussion phase and equal voting rights without narrating the earlier vote.

A first live check (session 24) exposed another bypass, “proceed to choose myself.” That attempt failed and was abandoned through the normal disconnect grace period, with its records retained. The final shared guard checks bounded first-person election-choice clauses, including this variant, and false eligibility claims. It runs during draft validation, before saving AI chat, and when loading historical public/model transcripts. It does not alter human messages or private ballot eligibility. Invalid drafts can regenerate once within the existing time budget; repeated invalid output stays silent with its reason logged.

Final checks used the unchanged `[human, ai, ai]` configuration and 60-second timer:

| Check | Actual result |
| --- | --- |
| Full backend unit suite, including screenshot and live-bypass variants | 99 tests passed; controlled provider responses cover both models and rounds, correction, persistent rejection, pre-save rejection, historical filtering, and legal self-votes |
| Fresh Qwen session 25, default Docker model | Both rounds completed, including empty Round 2 openings, 10-second break/readiness, four live model ballots, final result, and result refresh |
| Qwen Round 2 direct basis/honesty question | Generated with 31.096 seconds remaining, posted after 6.657 seconds; second direct follow-up posted after 9.986 seconds from a 12.389-second snapshot |
| Fresh Windows Llama session 26 | Both rounds completed with empty Round 2 openings, direct questions, same-round follow-ups, four live model ballots, and final browser result |
| Llama Round 2 basis/honesty question and follow-up | Generated with 39.891 and 34.883 seconds remaining; posted after 4.963 and 4.923 seconds |
| Llama eligibility question | Confirmed every player may self-vote, without naming its private choice; posted after 5.503 seconds from a 15.624-second snapshot |
| Final-run public ballot announcements / false ineligibility | None found in either stored transcript; published replies and all eight AI ballots were attributed to the configured live model, not a fallback |
| Very late Qwen Round 1 follow-up | Sent with about five seconds left, timed out and stayed silent; voting opened on schedule |

These runs combined actual browser interactions/observations with normal HTTP/WebSocket test actions. No session phase, deadline, ballot, or result was injected into PostgreSQL. The backend was rebuilt for each mode; cached models were reused without downloads. The default reviewer mode was restored after testing. Existing completed and abandoned sessions and `ai_prompts.md` were preserved.

The live checks are finite examples, not a guarantee against all hallucinations or paraphrased disclosures. In particular, the filters do not prove the models' general factual accuracy or conversational quality.

## Windows demo startup recovery

The supplied startup log was reproduced: images built successfully, but FastAPI failed at `GET /api/tags` because the Windows Ollama API server was stopped. A remaining `ollama runner` belonged to the earlier temporary test server; it did not listen on API port 11434. Windows localhost and a one-off backend container both failed to connect before the server was restored.

Added `start-demo.ps1` to check the effective Compose model configuration, reuse a running Windows server or start the installed `ollama serve` on localhost, reject a missing model without a download, and run the existing Compose override. The server remains running after the helper finishes. The backend now retries temporary connection failures with bounded delays and distinct connection/model/load errors. The Windows override gives its healthcheck a 180-second startup grace period. Reviewer startup and model-download configuration are unchanged.

Checks actually run:

- `./start-demo.ps1 -Detached` from the stopped-server condition: started Windows Ollama, reused installed `llama3:latest`, built the app, and reached `Windows Ollama ready model=llama3:latest` and `Application ready` with the URL.
- `powershell.exe -NoProfile -File ./start-demo.ps1 -Detached`: passed in Windows PowerShell, reused the same server process and cached images, and reached readiness again.
- A temporary process-only `OLLAMA_MODEL=chm-not-installed-startup-check`: rejected before Compose startup, with no model pull. The environment was restored.
- Complete backend suite using the documented read-only `/checks` mount: **106 tests passed**. Ten startup tests cover delayed availability, exhausted retries, model absence, invalid configuration/responses, and warmup failure. Delayed/exhausted retry timing was tested with mocked providers, not by disrupting the healthy demo server.
- An actual call through `LLMPlayer` inside the running backend produced a Llama discussion reply in **5.124 seconds** and a valid structured model vote in **2.293 seconds**. Both diagnostics showed `source=model provider=ollama model=llama3:latest`; the private choice was not printed or saved to a participant session.
- Frontend HTTP **200**, proxied backend/database health **ok**, and Windows Ollama reachable from Docker while listening only on Windows `127.0.0.1:11434`.
- Both Compose configurations retained their intended model defaults: reviewer `qwen3:1.7b`, Windows demo `llama3:latest`. No model download or volume deletion occurred; existing sessions and `ai_prompts.md` were preserved.

Files changed for this startup fix: `start-demo.ps1`, `compose.ollama.yml`, `backend/app/main.py`, `backend/tests/test_ollama_startup.py`, `README.md`, `docs/troubleshooting.md`, and this record. The demo mode was left running. A full two-round browser session was not repeated for this startup-only change; earlier two-round model checks are recorded above.

## Study grounding, private pacing, and voting evidence

The reported session 33 was traced in PostgreSQL and backend logs. Its game-history and countdown messages were live `llama3:latest` discussion output, not fallback chat or a private vote. The calls had 36.585 and 21.650 seconds remaining when they began; the model repeated rounded snapshot values as if they were discussion content. The implementation supplied current-round chat, but the instruction did not explicitly establish that no game/task had occurred or restrict the timer to pacing.

Discussion now states the actual anonymous study context: only chat and voting have occurred, the app counts private ballots to select a Decision Maker, and only the later experiment is hypothetical. Outside biographies and task performance are not supplied. Time remains in every request for pacing and server diagnostics, while generated countdown readings and the reported invented-game-history constructions are rejected and may regenerate once. The server's deadline, late-reply rejection, and private-ballot rules remain in force.

The voting audit also found a concrete input-coverage problem: the previous newest-message caps and larger current-round budget could remove an early or quieter player's contribution. `select_vote_evidence` now considers all eligible public messages and reserves equal space per player and included round within the same 3,000-byte budget. It prioritizes initial/recent complete messages and shares unused capacity toward less represented groups, then restores chronological order. The model is told when this is an excerpt and instructed against rewarding volume, confident claims, clock pressure, or invented game achievements. Aliases, identity hiding, self-vote eligibility, ballot validation, and election rules remain unchanged.

Actual checks:

- Complete backend suite: **119 tests passed**. New checks include exact reported clock/game drafts, regeneration or silence, study facts in both model configurations/rounds, and eight evidence-selection tests (quiet early contributor, every round/player, all six label permutations, complete UTF-8 messages and finite-budget omissions).
- Fresh live Llama session 34: both 60-second rounds, two openings per round, a human question and direct follow-up in each, late human points, break/readiness, all six ballots, final result and browser refresh passed. Round 2 used the exact “so on what basis would you all be choosing decisiona maker?” at 36.321 seconds remaining; its reply posted after 5.332 seconds. The honesty follow-up posted after 6.894 seconds from a 30.920-second snapshot; the late uncertainty point posted after 7.080 seconds from 13.459 seconds remaining. All published chat and four AI ballots were `source=model`. Optional peer replies interrupted by newer human input were logged as superseded. No repeated countdown or fabricated game-history construction appeared in this run.
- After the final factual clarification and additional game-history guard, both Llama and cached Qwen were called again through the actual model adapter with the exact Round 2 question. Both returned model-generated replies without countdown narration or the reported invented history. The final clarification was not followed by another complete browser session.
- Qwen additionally generated a late honesty reply with a 12-second budget, without narrating the timer. Its full two-round browser flow was not rerun for this change.

For a small live voting comparison, the same three synthetic contributions were used repeatedly: one offered concrete evidence-seeking, uncertainty and correction; the others requested votes or claimed superiority without reasons. The stronger contribution moved across A/B/C, with forward/reversed transcript order (six cases per model). The test process fixed the private alias mapping to isolate these label/order changes; production still shuffles aliases. No participant ballots were created for these fixtures.

| Model | Selected stronger contribution | Provider failures/fallbacks |
| --- | --- | --- |
| Windows `llama3:latest` | 6 / 6 | 0 |
| Cached reviewer `qwen3:1.7b` | 5 / 6 | 0 |

Qwen selected a weaker contribution in the strongest-C/reversed-order case. This is an observed limitation, not a passed fairness check. Neither this small fixture nor randomized aliases establishes unbiased judgment. Both seats share a model and criteria; votes can be correlated. Finite evidence can still omit oversized messages, and only a candidate is returned—not per-player scores or an auditable justification. Provider failures still produce a logged uniform random fallback rather than a transcript-based decision.

Files changed: `backend/app/llm_player.py`, new `backend/app/vote_context.py`, `backend/tests/test_llm_player.py`, new `backend/tests/test_vote_context.py`, `README.md`, and this record. The app was rebuilt with the Windows Compose override; cached Qwen was tested separately without switching the live demo or downloading models. Windows Llama demo mode, the 60-second configuration, saved sessions, model caches and `ai_prompts.md` were preserved.

## Submission UI, startup modes, and editable duration

Effective Compose configuration was checked for all requested commands:

| Command | Effective model |
| --- | --- |
| `docker compose up --build` | Bundled `qwen3:1.7b` |
| `docker compose -p chm-review-verify up --build` | Same project and bundled `qwen3:1.7b`; the project flag does not choose a model |
| `docker compose -f compose.yml -f compose.ollama.yml up --build` | Windows `llama3:latest` by default; explicit environment overrides are respected |

The current YAML was `[human, human, human]` / 60 when this work started and was restored byte for byte afterward. Models are prepared for readiness, but AI player turns require an `ai` seat. Duration editing already worked through the existing validated YAML, saved session settings, server deadlines, and React countdown; no timer logic change was needed.

Fresh reviewer model checks used a separate project, `chm-fresh-ui-20261120`, and a new empty model volume. An initially empty `ollama list` was followed by the existing initializer's `Downloading qwen3:1.7b`, a successful approximately 1.4 GB first-attempt pull with digest verification, a real response, and `Ready: qwen3:1.7b`. Repeating initialization printed `Using cached qwen3:1.7b` and reached readiness in 2.55 seconds. No TLS/proxy failure occurred in this test.

The same isolated project then started the complete reviewer stack with a fresh PostgreSQL volume and that newly downloaded model cache. A temporary port-only override used `127.0.0.1:3001` so the existing app/data/tokens on port 3000 were untouched. The app returned HTTP 200, proxied backend/database health was `ok`, and frontend readiness completed. Its actual Qwen adapter produced a model reply in 9.023 seconds and a valid vote in 6.196 seconds, with no fallback or participant records created. This was a fresh model-initialization check followed by a full-stack cached start, not one uninterrupted empty-cache full-stack invocation. Only the isolated project's containers were stopped afterward; its new test volumes and all existing volumes were retained.

The supplied `black-particle-waves.svg` was copied unchanged into `frontend/src/assets` and bundled by Vite. Both copies had SHA-256 `0D887C46815B5CD1F8BDC501C9B8D5F3FD212DAA91D5B84D774AD045C878A00A`. The UI now uses dark transparent surfaces, frosted panels, subtle borders, white/grey text, local system fonts and the user's SVG. No new dependencies, external fonts, model information, participant-type indicators, or experiment-flow changes were added.

Checks actually run:

- Frontend Docker/Vite production build passed, including the bundled SVG.
- Complete backend suite: **124 tests passed**, including five configuration tests for supported compositions, integer durations, invalid values and both-round session deadlines.
- `node --test startup-ready.test.mjs`: **4 tests passed**.
- Temporarily set the actual mounted YAML to 100 seconds and restarted the backend. Three independent browser players completed session 41 through both discussion/voting rounds, break/readiness and results using normal UI/WebSocket actions. Both rounds visibly began at **1:40**. The observed initial server durations were 99.867 and 99.975 seconds after connection overhead; voting opened on the stored deadlines. The completed session retained its saved 100-second setting after the YAML was restored to 60.
- Visually checked join, waiting, discussion, voting, readiness and results. Chat submission, keyboard radio selection, both votes, readiness after a refreshed break, and result refresh worked. Six ballots and the saved final result remained in PostgreSQL after restart.
- At a 320-pixel test viewport, join, discussion, voting and results had no horizontal overflow. A small initial minimum-width/margin overflow was fixed before the completed flow. Temporary viewport overrides were reset.
- The browser rendered the bundled SVG, `rgba(255, 255, 255, 0.035)` panel surfaces and `blur(16px)` frosting; the checked browser console had no errors.
- Final Windows-mode app on port 3000 remained healthy, using `llama3:latest`; the original three-human / 60-second YAML was restored. Existing completed results, caches and prompt-log entries were preserved.

Representative commands were the Windows-mode Compose build, the read-only backend test command above, `docker compose -f compose.yml -f compose.ollama.yml run --rm --no-deps -T --volume "${PWD}/frontend:/checks:ro" --workdir /checks frontend node --test startup-ready.test.mjs`, and the isolated project's real `ollama-init` service and full-stack start with a temporary port override.

Files changed: `frontend/src/main.jsx`, `frontend/src/style.css`, new `frontend/src/assets/black-particle-waves.svg`, new `backend/tests/test_config.py`, `README.md`, and this record. Quick manual check: edit `discussion_seconds` to 100, restart the backend with the matching Compose mode, then join a fresh session with the required humans and check for 1:40. Existing sessions keep the settings with which they began. First-start image/model downloads on another machine still depend on that machine's working registry/network access; those conditions cannot be guaranteed by a successful local run.
