"""BeHive API Server — FastAPI app for research missions."""

import os
import re
import time
import json
import hashlib
import asyncio
from collections import defaultdict, Counter
from contextlib import asynccontextmanager, suppress
from typing import Optional, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, Depends, Query
from behive import __version__ as BEHIVE_VERSION
from behive.config import load_project_env

load_project_env()

# ─── Concurrency control ──────────────────────────────────────────────────────
_MAX_CONCURRENT_MISSIONS = int(os.environ.get("BEHIVE_MAX_CONCURRENT", "3"))
_mission_semaphore: asyncio.Semaphore = None  # initialized in lifespan

def _get_semaphore() -> asyncio.Semaphore:
    global _mission_semaphore
    if _mission_semaphore is None:
        _mission_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MISSIONS)
    return _mission_semaphore
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

# Install legacy import shims
try:
    from behive.compat.shims import install_shims, install_ops_shim
    install_shims()
    install_ops_shim()
except ImportError:
    pass

# ─── DB connection ────────────────────────────────────────────────────────────

def get_db_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if os.environ.get("BEHIVE_DB_URL"):
        return os.environ["BEHIVE_DB_URL"]
    # Construct from individual vars
    user = os.environ.get("HIVE_PG_USER", "behive")
    password = os.environ.get("HIVE_PG_PASSWORD", "behive2026")
    host = os.environ.get("HIVE_PG_HOST", "localhost")
    port = os.environ.get("HIVE_PG_PORT", "5433")
    db = os.environ.get("HIVE_PG_DATABASE", "behive")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def get_db_url_display():
    """Return DB URL with password masked — for logs and health endpoint."""
    url = get_db_url()
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)


def get_db():
    """Connect to PostgreSQL. Returns connection or raises with helpful message."""
    try:
        import psycopg2
    except ImportError:
        raise RuntimeError(
            "psycopg2 not installed. Run: pip install 'behive[postgres]' or pip install psycopg2-binary"
        )
    url = get_db_url()
    try:
        return psycopg2.connect(url)
    except psycopg2.OperationalError as e:
        err_msg = str(e)
        raise RuntimeError(
            f"Cannot connect to PostgreSQL. {err_msg.strip()}\n\n"
            "Quick setup:\n"
            "  1. Install PostgreSQL: sudo apt install postgresql\n"
            "  2. Create database: behive init-db\n"
            "  3. Or set DATABASE_URL environment variable\n"
            f"\n  Tried: {get_db_url_display()}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Database error: {e}") from e


# ─── Auth ─────────────────────────────────────────────────────────────────────

def verify_auth(request: Request):
    """Check API key if BEHIVE_API_KEY is set."""
    api_key = os.environ.get("BEHIVE_API_KEY")
    if not api_key:
        return  # No auth required
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {api_key}":
        return
    # Also check X-API-Key header
    if request.headers.get("X-API-Key") == api_key:
        return
    raise HTTPException(401, "Invalid or missing API key")


# ─── Rate Limiting ────────────────────────────────────────────────────────────

class TokenBucket:
    def __init__(self, rate: int, per: int = 60):
        self.rate = rate
        self.per = per
        self.buckets: dict[str, list] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        bucket = self.buckets[key]
        # Remove old entries
        self.buckets[key] = [t for t in bucket if now - t < self.per]
        if len(self.buckets[key]) >= self.rate:
            return False
        self.buckets[key].append(now)
        return True


_rate_limiter = TokenBucket(rate=60, per=60)
_research_limiter = TokenBucket(rate=5, per=60)


# ─── App ──────────────────────────────────────────────────────────────────────

_continuous_discovery_task: asyncio.Task | None = None


async def _continuous_discovery_loop():
    """Advance continuous projects on their configured cadence without user prompts."""
    await asyncio.sleep(15)
    while True:
        try:
            from behive.engine.frontier import ensure_schema
            await asyncio.to_thread(ensure_schema)
            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT ON (p.id) m.id,p.cadence_minutes
                FROM hive_projects p JOIN hive_missions m ON LOWER(TRIM(m.topic))=LOWER(TRIM(p.root_question))
                LEFT JOIN hive_discovery_cycles dc ON dc.mission_id=m.id
                WHERE COALESCE(p.agent_policy->>'autonomy','assisted')='continuous'
                  AND m.status IN ('done','insufficient')
                GROUP BY p.id,m.id,p.cadence_minutes,m.created_at
                HAVING MAX(dc.completed_at) IS NULL OR
                       MAX(dc.completed_at) < NOW()-(GREATEST(15,p.cadence_minutes)||' minutes')::interval
                ORDER BY p.id,m.created_at DESC
                LIMIT 2
            """)
            due = cur.fetchall(); conn.close()
            for mission_id, _cadence in due:
                task = _frontier_tasks.get(mission_id)
                if not task or task.done():
                    _frontier_tasks[mission_id] = asyncio.create_task(
                        _run_controller_background(mission_id, "continuous_cadence"))
        except Exception as exc:
            print(f"Continuous discovery scheduler warning: {exc}")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — check DB connectivity on startup."""
    print("🐝 BeHive API starting...")
    # Validate DB connection at startup (non-fatal — API still serves /health as degraded)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.close()
        print("✅ PostgreSQL connected")
    except RuntimeError as e:
        print(f"⚠️  Database not available: {e}")
        print("   The API will start but /research will fail until DB is configured.")
        print("   Run: behive init-db")
    except Exception as e:
        print(f"⚠️  Database check failed: {e}")
    global _continuous_discovery_task
    _continuous_discovery_task = asyncio.create_task(_continuous_discovery_loop())
    yield
    if _continuous_discovery_task:
        _continuous_discovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await _continuous_discovery_task
    print("🐝 BeHive API shutdown.")


app = FastAPI(
    title="BeHive Research Engine",
    description="Deep research with structured knowledge extraction",
    version=BEHIVE_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("BEHIVE_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_web_dir = Path(__file__).parent / "web"
if _web_dir.exists():
    app.mount("/app/assets", StaticFiles(directory=_web_dir), name="research-app-assets")

    @app.get("/app", include_in_schema=False)
    async def research_workspace():
        return FileResponse(_web_dir / "index.html")

# ─── Free Tools Routers ──────────────────────────────────────────────────────
try:
    from behive.brand_check import router as brand_check_router
    app.include_router(brand_check_router)
except ImportError:
    pass

# ─── Federated Knowledge Network ─────────────────────────────────────────────
try:
    from behive.federation import router as federation_router
    app.include_router(federation_router)
except ImportError:
    pass

# Living research projects: evolving question trees and discovery frontiers.
try:
    from behive.discovery_api import router as discovery_router
    app.include_router(discovery_router)
except ImportError:
    pass


# ─── SSE Event Bus ────────────────────────────────────────────────────────────

_mission_events: dict[str, list[dict]] = defaultdict(list)
_mission_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
_mission_runtime: dict[str, dict] = {}
_gap_processes: dict[int, asyncio.subprocess.Process] = {}


def _emit_event(mission_id: str, event: str, data: dict):
    """Push SSE event to store and notify subscribers."""
    entry = {"event": event, "data": data, "ts": time.time()}
    events = _mission_events[mission_id]
    events.append(entry)
    if len(events) > 500:
        _mission_events[mission_id] = events[-500:]
    for q in _mission_subscribers.get(mission_id, []):
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass


# ─── Security Middleware ──────────────────────────────────────────────────────

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    if not _rate_limiter.is_allowed(client_ip):
        return JSONResponse(status_code=429, content={"detail": "Too many requests. Max 60/min."}, headers={"Retry-After": "60"})
    if request.url.path == "/research" and request.method == "POST":
        if not _research_limiter.is_allowed(client_ip):
            return JSONResponse(status_code=429, content={"detail": "Research rate limit: max 5/min."}, headers={"Retry-After": "60"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ─── Models ───────────────────────────────────────────────────────────────────

class ResearchRequest(BaseModel):
    query: str = Field(default="", description="Research topic or question")
    topic: str = Field(default="", description="Alias for query (backward compat)")
    depth: int = Field(default=3, ge=1, le=5, description="1=quick, 3=standard, 5=deep")
    scale: int = Field(default=200, ge=10, le=500, description="Max sources to scout")
    force: bool = Field(True, description="Skip dedup check")
    
    @property
    def effective_query(self) -> str:
        return self.query or self.topic


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = BEHIVE_VERSION
    missions_total: int = 0
    claims_total: int = 0
    avg_quality: float = 0.0
    db: str = ""


class GapUpdate(BaseModel):
    question: str = Field(..., min_length=8, max_length=1000)


# ─── Entity Extraction (lightweight NLP) ─────────────────────────────────────

_ENTITY_PATTERN = re.compile(
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:Inc|Corp|Ltd|LLC|AG|SA|GmbH|Co|plc)\.?)?)\b'
    r'|'
    r'\b([A-Z]{2,}(?:\s+[A-Z]{2,})*)\b'  # ALL CAPS (acronyms like NVIDIA, EU, AI)
    r'|'
    r'\$[\d,.]+\s*(?:billion|million|trillion|B|M|T)\b'  # Money amounts
)

_STOP_ENTITIES = {
    "The", "This", "That", "These", "Those", "However", "Although",
    "According", "Furthermore", "Additionally", "Meanwhile", "Moreover",
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
}


def extract_entities(text: str) -> list[str]:
    """Extract entity names from claim text.
    
    Primary: uses hive_entities table (LLM-extracted, typed).
    Fallback: improved regex for when DB lookup isn't available.
    """
    entities = set()
    for match in _ENTITY_PATTERN.finditer(text):
        entity = match.group(0).strip()
        if entity and len(entity) > 1 and entity not in _STOP_ENTITIES:
            # Filter out garbage: pure uppercase common words
            if entity.isupper() and len(entity) <= 3 and entity not in {
                "AI", "EU", "US", "UK", "UN", "AWS", "GCP", "IBM", "MIT", "GDP",
                "CEO", "CTO", "API", "LLM", "GPU", "CPU", "RAM", "SQL", "IoT",
            }:
                continue
            entities.add(entity)
    return sorted(entities)


def _get_typed_entities_for_claim(cur, claim_text: str, mission_id: str) -> list[dict]:
    """Get LLM-extracted typed entities from hive_entities table."""
    cur.execute("""
        SELECT DISTINCT entity_type, value 
        FROM hive_entities 
        WHERE mission_id = %s 
          AND (value ILIKE %s OR %s ILIKE '%%' || value || '%%')
        LIMIT 50
    """, (mission_id, f"%{claim_text[:100]}%", claim_text[:200]))
    return [{"type": r[0], "name": r[1]} for r in cur.fetchall()]


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

# ─── 1. Health ────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        missions = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE is_garbage = false")
        row = cur.fetchone()
        claims, avg_q = row[0], row[1] or 0
        conn.close()
        return HealthResponse(
            missions_total=missions,
            claims_total=claims,
            avg_quality=round(float(avg_q), 4),
            db=get_db_url().split("@")[-1] if "@" in get_db_url() else "connected",
        )
    except Exception as e:
        return HealthResponse(status="degraded", db=str(e)[:200])


# ─── 2. Start Research ────────────────────────────────────────────────────────

@app.post("/research", dependencies=[Depends(verify_auth)])
async def start_research(req: ResearchRequest):
    """Start a new research mission. Returns job_id for polling."""
    if not req.effective_query:
        raise HTTPException(status_code=422, detail="Either 'query' or 'topic' must be provided")
    ts = str(int(time.time()))
    h = hashlib.md5(f"{req.effective_query}{ts}".encode()).hexdigest()[:6]
    mission_id = f"hive_{ts}_{h}"

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO hive_missions (id, topic, status, phase, config, created_at)
            VALUES (%s, %s, 'queued', 'queued', %s::jsonb, NOW())
            ON CONFLICT (id) DO NOTHING
        """, (mission_id, req.effective_query, json.dumps({"depth": req.depth, "scale": req.scale})))
        conn.commit()
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    asyncio.create_task(_run_pipeline(mission_id, req.effective_query, req.depth))

    return {
        "job_id": mission_id,
        "status": "running",
        "topic": req.effective_query,
        "depth": req.depth,
        "message": f"Research started. Poll GET /research/{mission_id}/status or stream GET /research/{mission_id}/events",
    }


# ─── 3. Research Status ──────────────────────────────────────────────────────

@app.get("/research/{mission_id}/status")
async def research_status(mission_id: str):
    """Get mission status, progress, and watchdog diagnostics."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT status,phase,topic,EXTRACT(EPOCH FROM NOW()-created_at),COALESCE(quality_metrics,'{}'::jsonb) "
            "FROM hive_missions WHERE id = %s", (mission_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        status, phase, topic, mission_age, diagnostics = row
        # Get current claims count
        cur.execute(
            "SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)",
            (mission_id,)
        )
        crow = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM hive_sources WHERE mission_id = %s", (mission_id,))
        source_count = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM hive_content WHERE mission_id = %s AND word_count > 0", (mission_id,))
        content_count = cur.fetchone()[0] or 0
        conn.close()
        claims_count = crow[0] or 0
        age_seconds = max(0, int(float(mission_age or 0)))
        runtime = _mission_runtime.get(mission_id)
        run_age_seconds = max(0, int(time.time() - runtime["started_at"])) if runtime and runtime.get("started_at") else age_seconds
        terminal = status in ("done", "insufficient", "error", "cancelled", "interrupted")
        checks = []

        if runtime and runtime.get("last_output_at"):
            silent_seconds = max(0, int(time.time() - runtime["last_output_at"]))
            heartbeat_state = "pass" if silent_seconds <= 180 else "warn" if silent_seconds <= 600 else "fail"
            heartbeat_detail = f"Worker output {silent_seconds}s ago"
        elif terminal:
            heartbeat_state, heartbeat_detail = "pass", "Worker exited"
            silent_seconds = None
        else:
            heartbeat_state = "unknown" if age_seconds <= 300 else "fail"
            heartbeat_detail = "No server heartbeat available" if age_seconds > 300 else "Heartbeat starting"
            silent_seconds = None
        checks.append({"id": "heartbeat", "label": "Worker heartbeat", "state": heartbeat_state, "detail": heartbeat_detail})

        pipeline_phases = ("planning", "scout", "harvest", "process", "falsify", "synth", "graph")
        inconsistent = status in pipeline_phases and phase in pipeline_phases and status != phase
        phase_state = "fail" if inconsistent and age_seconds > 600 else "warn" if inconsistent else "pass"
        phase_detail = f"Status {status} disagrees with phase {phase}" if inconsistent else f"State synchronized at {phase or status}"
        checks.append({"id": "phase", "label": "Phase consistency", "state": phase_state, "detail": phase_detail})

        downstream = status in ("process", "falsify", "synth", "graph", "done", "partial")
        worker_stale = bool(runtime and runtime.get("last_output_at") and time.time() - runtime["last_output_at"] > 600)
        flow_failed = source_count > 0 and claims_count == 0 and (
            terminal and status not in ("insufficient",) or worker_stale or
            (not terminal and not runtime and age_seconds > 900)
        )
        flow_state = "fail" if flow_failed else "warn" if status == "insufficient" else "pass" if claims_count > 0 else "pending"
        flow_detail = f"{source_count} sources → {content_count} documents → {claims_count} findings"
        checks.append({"id": "flow", "label": "Source-to-finding flow", "state": flow_state, "detail": flow_detail})

        result_state = "warn" if status == "insufficient" else "fail" if status == "done" and claims_count == 0 else "pass" if claims_count > 0 else "pending"
        result_detail = ("No defensible findings met the evidence threshold" if status == "insufficient" else
                         f"{claims_count} findings produced" if claims_count else "No findings produced yet")
        checks.append({"id": "results", "label": "Result production", "state": result_state, "detail": result_detail})

        completion_state = "pass" if status in ("done", "insufficient") else "fail" if status in ("error", "cancelled", "interrupted") or (not terminal and run_age_seconds > 3600) else "pending"
        completion_detail = ("Mission completed" if status == "done" else
                             "Mission completed with insufficient evidence" if status == "insufficient" else
                             f"Mission ended: {status}" if terminal else f"Current attempt active for {run_age_seconds // 60}m")
        checks.append({"id": "completion", "label": "Full mission completion", "state": completion_state, "detail": completion_detail})
        severity = {"unknown": 0, "pending": 1, "pass": 1, "warn": 2, "fail": 3}
        watchdog_state = max(checks, key=lambda item: severity[item["state"]])["state"]
        return {
            "mission_id": mission_id,
            "status": status,
            "phase": phase,
            "topic": topic,
            "claims_so_far": claims_count,
            "sources_so_far": source_count,
            "documents_so_far": content_count,
            "avg_quality": round(float(crow[1] or 0), 4),
            "diagnostics": diagnostics or {},
            "watchdog": {"state": watchdog_state, "mission_age_seconds": age_seconds,
                         "run_age_seconds": run_age_seconds,
                         "silent_seconds": silent_seconds, "checks": checks},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/research/{mission_id}/recover")
async def recover_research(mission_id: str):
    """Resume an incomplete mission from the first missing durable checkpoint."""
    try:
        conn = get_db()
        cur = conn.cursor()
        running_task = _mission_runtime.get(mission_id)
        if running_task and running_task.get("returncode") is None:
            raise HTTPException(409, "This mission already has an active worker")
        cur.execute("SELECT topic,COALESCE(quality_metrics,'{}'::jsonb) FROM hive_missions WHERE id=%s", (mission_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        cur.execute("SELECT COUNT(*) FROM hive_sources WHERE mission_id=%s", (mission_id,))
        sources = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM hive_content WHERE mission_id=%s AND word_count>0", (mission_id,))
        content = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM hive_claims WHERE mission_id=%s", (mission_id,))
        claims = cur.fetchone()[0] or 0
        cur.execute("SELECT LENGTH(COALESCE(synthesis,'')) FROM hive_missions WHERE id=%s", (mission_id,))
        synthesis_length = cur.fetchone()[0] or 0
        stage = "scout" if sources == 0 else "harvest" if content == 0 else "process" if claims == 0 else "synth" if synthesis_length < 100 else "done"
        if stage == "done":
            conn.close()
            return {"mission_id": mission_id, "status": "done", "recovery_stage": "none"}
        previous_metrics = row[1] or {}
        recovery_attempt = int(previous_metrics.get("recovery_attempt", 0)) + 1
        cur.execute(
            "UPDATE hive_missions SET status=%s,phase=%s,quality_metrics="
            "(COALESCE(quality_metrics,'{}'::jsonb)-'error_message')||jsonb_build_object("
            "'recovery_attempt',%s,'recovery_stage',%s,'recovery_started_at',NOW()) WHERE id=%s",
            (stage, stage, recovery_attempt, stage, mission_id),
        )
        conn.commit()
        conn.close()
        asyncio.create_task(_run_recovery(mission_id, stage))
        return {"mission_id": mission_id, "status": "recovering", "recovery_stage": stage,
                "attempt": recovery_attempt,
                "checkpoints": {"sources": sources, "content": content, "claims": claims},
                "message": f"Reusing {sources} sources and {content} documents; resuming at {stage}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 4. SSE Events Stream ────────────────────────────────────────────────────

@app.get("/research/{mission_id}/events")
async def research_events(mission_id: str):
    """Server-Sent Events stream for a research mission."""

    async def event_generator() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        _mission_subscribers[mission_id].append(queue)
        try:
            # Send any existing events first (replay)
            for entry in _mission_events.get(mission_id, []):
                yield f"event: {entry['event']}\ndata: {json.dumps(entry['data'])}\n\n"

            # Stream new events
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: {entry['event']}\ndata: {json.dumps(entry['data'])}\n\n"
                    if entry["event"] in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield f": keepalive\n\n"
        finally:
            _mission_subscribers[mission_id].remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ─── 5. Get Full Research Results ─────────────────────────────────────────────

@app.get("/research/{mission_id}")
async def get_research(mission_id: str):
    """Get completed research results with claims."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT topic, status, phase, synthesis FROM hive_missions WHERE id = %s", (mission_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        topic, status, phase, report = row
        cur.execute("""
            SELECT claim, confidence, quality_score, source_url, claim_type
            FROM hive_claims
            WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC LIMIT 500
        """, (mission_id,))
        claims = [
            {"text": r[0], "confidence": r[1], "quality_score": r[2], "source_url": r[3], "type": r[4]}
            for r in cur.fetchall()
        ]
        conn.close()
        avg_q = sum(c["quality_score"] or 0 for c in claims) / max(1, len(claims))
        return {
            "mission_id": mission_id,
            "topic": topic,
            "status": status,
            "phase": phase,
            "report": report or "",
            "claims": claims,
            "metrics": {"total_claims": len(claims), "avg_quality": round(avg_q, 4)},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 6. Get Report Only ──────────────────────────────────────────────────────

@app.get("/research/{mission_id}/report")
async def get_report(mission_id: str):
    """Get synthesized report for a completed mission (no claims array)."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT topic, status, synthesis FROM hive_missions WHERE id = %s", (mission_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        topic, status, synthesis = row
        cur.execute(
            "SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)",
            (mission_id,)
        )
        crow = cur.fetchone()
        conn.close()
        if status not in ("done", "insufficient"):
            raise HTTPException(202, f"Mission still in progress (status={status})")
        return {
            "mission_id": mission_id,
            "topic": topic,
            "synthesis": synthesis or "",
            "claims_count": crow[0] or 0,
            "avg_quality": round(float(crow[1] or 0), 4),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 7. Search Claims (primary path) ─────────────────────────────────────────

@app.get("/research/{mission_id}/evidence")
async def get_research_evidence(mission_id: str):
    """Return the mission library and a deduplicated bibliography."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM hive_missions WHERE id=%s", (mission_id,))
        if not cur.fetchone():
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        cur.execute("""
            SELECT c.title, c.url, c.domain, c.word_count, c.harvest_method,
                   c.quality_score, c.author, c.published_date, c.harvested_at,
                   COUNT(cl.id) AS claim_count, MIN(s.evidence_tier),
                   MAX(s.verification_status), MAX(s.persistent_id),
                   BOOL_OR(COALESCE(s.is_primary,FALSE)), BOOL_OR(COALESCE(s.is_open_access,FALSE))
            FROM hive_content c
            LEFT JOIN hive_claims cl ON cl.mission_id=c.mission_id AND cl.source_url=c.url
              AND (cl.is_garbage=false OR cl.is_garbage IS NULL)
            LEFT JOIN hive_sources s ON s.mission_id=c.mission_id AND s.url=c.url
            WHERE c.mission_id=%s
            GROUP BY c.id, c.title, c.url, c.domain, c.word_count, c.harvest_method,
                     c.quality_score, c.author, c.published_date, c.harvested_at
            ORDER BY COUNT(cl.id) DESC, c.quality_score DESC, c.harvested_at DESC
            LIMIT 500
        """, (mission_id,))
        library = [{"title": r[0] or r[2] or r[1], "url": r[1], "domain": r[2] or "",
                    "word_count": r[3] or 0, "harvest_method": r[4] or "unknown",
                    "quality_score": float(r[5] or 0), "author": r[6] or "",
                    "published_date": r[7] or "", "harvested_at": r[8].isoformat() if r[8] else "",
                    "claim_count": r[9] or 0, "evidence_tier": r[10],
                    "verification_status": r[11] or "unverified", "persistent_id": r[12] or "",
                    "is_primary": bool(r[13]), "is_open_access": bool(r[14])} for r in cur.fetchall()]
        conn.close()
        bibliography = [{"number": i + 1, "title": item["title"], "url": item["url"],
                         "domain": item["domain"], "author": item["author"],
                         "published_date": item["published_date"], "accessed_at": item["harvested_at"],
                         "supports_findings": item["claim_count"], "evidence_tier": item["evidence_tier"],
                         "verification_status": item["verification_status"],
                         "persistent_id": item["persistent_id"], "is_primary": item["is_primary"]}
                        for i, item in enumerate(library)]
        return {"mission_id": mission_id, "library": library, "bibliography": bibliography,
                "metrics": {"documents": len(library), "cited_sources": sum(x["claim_count"] > 0 for x in library),
                            "registry_verified": sum(x["verification_status"] == "registry_verified" for x in library),
                            "tier_one": sum(x["evidence_tier"] == 1 for x in library)}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


_frontier_tasks: dict[str, asyncio.Task] = {}


class DiscoveryStateUpdate(BaseModel):
    status: str = Field(pattern="^(open|investigating|paused|rejected|supported|superseded|resolved)$")


async def _run_frontier_background(mission_id: str, trigger: str):
    try:
        from behive.engine.frontier import run_frontier_cycle
        _emit_event(mission_id, "discovery", {"state": "mapping", "message": "Mapping observations and evidence edges"})
        result = await asyncio.to_thread(run_frontier_cycle, mission_id, trigger)
        _emit_event(mission_id, "discovery", {"state": "complete", **result})
    except Exception as exc:
        _emit_event(mission_id, "discovery", {"state": "failed", "message": str(exc)})
    finally:
        _frontier_tasks.pop(mission_id, None)


async def _run_controller_background(mission_id: str, trigger: str):
    """Run one ranked continuous-research action and publish its outcome."""
    try:
        from behive.engine.controller import run_next_action
        _emit_event(mission_id, "controller", {"state": "ranking", "message": "Ranking branch actions by information gain"})
        result = await asyncio.to_thread(run_next_action, mission_id, trigger)
        _emit_event(mission_id, "controller", {"state": result.get("status", "complete"), **result})
    except Exception as exc:
        _emit_event(mission_id, "controller", {"state": "retrying", "message": str(exc), "recoverable": True})
    finally:
        _frontier_tasks.pop(mission_id, None)


@app.get("/research/{mission_id}/discovery")
async def get_discovery(mission_id: str):
    """Return the open-world evidence graph, unknowns, and hypothesis portfolio."""
    try:
        from behive.engine.frontier import get_discovery_map
        result = await asyncio.to_thread(get_discovery_map, mission_id)
        task = _frontier_tasks.get(mission_id)
        result["active"] = bool(task and not task.done())
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/research/{mission_id}/discovery/cycles")
async def start_discovery_cycle(mission_id: str):
    """Start another autonomous map-gap-hypothesize-discriminate cycle."""
    task = _frontier_tasks.get(mission_id)
    if task and not task.done():
        raise HTTPException(409, "A discovery cycle is already active")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM hive_missions WHERE id=%s", (mission_id,))
    exists = cur.fetchone(); conn.close()
    if not exists:
        raise HTTPException(404, f"Mission {mission_id} not found")
    _frontier_tasks[mission_id] = asyncio.create_task(_run_frontier_background(mission_id, "user_or_autonomous_cycle"))
    return {"mission_id": mission_id, "status": "queued", "mode": "open_world_discovery"}


@app.get("/research/{mission_id}/controller")
async def continuous_controller_state(mission_id: str):
    """Return branch memory, ranked action queue, and the durable change ledger."""
    try:
        from behive.engine.controller import get_controller_state
        return await asyncio.to_thread(get_controller_state, mission_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/research/{mission_id}/controller/cycles")
async def run_continuous_controller(mission_id: str):
    """Run the highest-value available branch action now."""
    task = _frontier_tasks.get(mission_id)
    if task and not task.done():
        raise HTTPException(409, "A discovery or controller cycle is already active")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM hive_missions WHERE id=%s", (mission_id,))
    exists = cur.fetchone(); conn.close()
    if not exists:
        raise HTTPException(404, f"Mission {mission_id} not found")
    _frontier_tasks[mission_id] = asyncio.create_task(_run_controller_background(mission_id, "user_controller_cycle"))
    return {"mission_id": mission_id, "status": "queued", "mode": "continuous_controller"}


@app.patch("/research/{mission_id}/frontiers/{frontier_id}")
async def update_frontier_state(mission_id: str, frontier_id: str, body: DiscoveryStateUpdate):
    from behive.engine.frontier import ensure_schema
    ensure_schema(); conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE hive_frontiers SET status=%s,updated_at=NOW() WHERE id=%s AND mission_id=%s RETURNING id",
                (body.status, frontier_id, mission_id))
    row = cur.fetchone(); conn.commit(); conn.close()
    if not row: raise HTTPException(404, "Frontier not found")
    return {"id": frontier_id, "status": body.status}


@app.patch("/research/{mission_id}/hypotheses/{hypothesis_id}")
async def update_hypothesis_state(mission_id: str, hypothesis_id: str, body: DiscoveryStateUpdate):
    from behive.engine.frontier import ensure_schema
    ensure_schema(); conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE hive_hypothesis_paths SET status=%s,updated_at=NOW() WHERE id=%s AND mission_id=%s RETURNING id",
                (body.status, hypothesis_id, mission_id))
    row = cur.fetchone(); conn.commit(); conn.close()
    if not row: raise HTTPException(404, "Hypothesis not found")
    return {"id": hypothesis_id, "status": body.status}


class HypothesisEvidenceUpdate(BaseModel):
    direction: str = Field(pattern="^(supports|weakens|contradicts|non_discriminating|context_limits)$")
    weight: float = Field(default=.5, ge=0, le=1)
    reason: str = Field(min_length=3, max_length=3000)
    evidence_edge_id: str = ""
    evidence_url: str = ""
    conditions: dict = Field(default_factory=dict)


class ExperimentStateUpdate(BaseModel):
    status: str = Field(pattern="^(proposed|queued|running|completed|inconclusive|cancelled)$")


@app.get("/research/{mission_id}/evolution")
async def get_hypothesis_evolution(mission_id: str):
    try:
        from behive.engine.evolution import get_evolution
        return await asyncio.to_thread(get_evolution, mission_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/research/{mission_id}/evolution/generations")
async def evolve_hypothesis_population(mission_id: str):
    try:
        from behive.engine.evolution import evolve_hypotheses
        result = await asyncio.to_thread(evolve_hypotheses, mission_id, "api_generation")
        return {"mission_id": mission_id, **result}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/research/{mission_id}/hypothesis-versions/{version_id}/evidence")
async def apply_hypothesis_evidence(mission_id: str, version_id: str, body: HypothesisEvidenceUpdate):
    try:
        from behive.engine.evolution import update_hypothesis
        return await asyncio.to_thread(update_hypothesis, mission_id, version_id, body.direction,
                                       body.weight, body.reason, body.evidence_edge_id,
                                       body.evidence_url, body.conditions)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/research/{mission_id}/counterfactual/{subject_id}")
async def inspect_counterfactual(mission_id: str, subject_id: str):
    try:
        from behive.engine.evolution import counterfactual
        return await asyncio.to_thread(counterfactual, mission_id, subject_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/research/{mission_id}/graph-audits")
async def run_graph_audit(mission_id: str):
    try:
        from behive.engine.evolution import audit_graph
        return await asyncio.to_thread(audit_graph, mission_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.patch("/research/{mission_id}/experiments/{experiment_id}")
async def update_experiment_state(mission_id: str, experiment_id: str, body: ExperimentStateUpdate):
    from behive.engine.evolution import ensure_schema
    ensure_schema(); conn=get_db(); cur=conn.cursor()
    cur.execute("UPDATE hive_discovery_experiments SET status=%s,updated_at=NOW() WHERE id=%s AND mission_id=%s RETURNING id",
                (body.status,experiment_id,mission_id))
    row=cur.fetchone(); conn.commit(); conn.close()
    if not row: raise HTTPException(404,"Experiment not found")
    return {"id":experiment_id,"status":body.status}


@app.get("/research/{mission_id}/analysis")
async def get_research_analysis(mission_id: str):
    """Return structured Analyst-core output without blending inference into findings."""
    try:
        from behive.engine.analyst import ANALYSIS_SCHEMA
        conn = get_db()
        cur = conn.cursor()
        cur.execute(ANALYSIS_SCHEMA)
        conn.commit()
        cur.execute("SELECT analysis,depth_status,source_count,cited_source_count,claim_count,updated_at FROM hive_analysis WHERE mission_id=%s", (mission_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            raise HTTPException(404, "Deep analysis has not been generated for this mission")
        return {"mission_id": mission_id, "analysis": row[0], "depth_status": row[1],
                "source_count": row[2], "cited_source_count": row[3], "claim_count": row[4],
                "updated_at": row[5].isoformat() if row[5] else ""}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/research/{mission_id}/gaps")
async def get_research_gaps(mission_id: str):
    """List Analyst gaps with their latest independent deepening checkpoint."""
    try:
        from behive.engine.deepening import ensure_schema
        ensure_schema()
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT g.id,g.gap_query,g.gap_type,g.priority,g.resolved,
                   r.id,r.child_mission_id,r.status,r.target_sources,r.target_primary,
                   r.full_text_sources,r.primary_sources,r.merged_claims,r.message,r.updated_at,
                   cm.status,cm.phase
            FROM hive_gaps g
            LEFT JOIN LATERAL (
              SELECT * FROM hive_gap_runs WHERE gap_id=g.id ORDER BY id DESC LIMIT 1
            ) r ON TRUE
            LEFT JOIN hive_missions cm ON cm.id=r.child_mission_id
            WHERE g.mission_id=%s AND COALESCE(g.gap_type,'')='analyst'
            ORDER BY g.resolved,g.priority DESC,g.id
        """, (mission_id,))
        rows = cur.fetchall(); conn.close()
        def displayed_status(row):
            if row[4]: return "resolved"
            stored = row[7] or "open"
            if stored not in {"queued", "scouting", "harvesting", "extracting", "evaluating"}: return stored
            phase = (row[15] or row[16] or "").lower()
            return ({"planning": "scouting", "running": "scouting", "scout": "scouting", "harvest": "harvesting",
                     "process": "extracting", "falsify": "evaluating", "synth": "evaluating", "analyze": "evaluating"}.get(phase, stored))
        return {"mission_id": mission_id, "gaps": [{
            "id": r[0], "question": r[1], "type": r[2], "priority": r[3], "resolved": r[4],
            "run_id": r[5], "child_mission_id": r[6], "status": displayed_status(r),
            "target_sources": r[8] or 3, "target_primary": r[9] or 1,
            "full_text_sources": r[10] or 0, "primary_sources": r[11] or 0,
            "merged_claims": r[12] or 0, "message": r[13] or "Ready for targeted research",
            "updated_at": r[14].isoformat() if r[14] else "",
        } for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.patch("/research/{mission_id}/gaps/{gap_id}")
async def edit_research_gap(mission_id: str, gap_id: int, body: GapUpdate):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE hive_gaps SET gap_query=%s,resolved=FALSE WHERE id=%s AND mission_id=%s RETURNING id",
                (body.question.strip(), gap_id, mission_id))
    if not cur.fetchone():
        conn.close(); raise HTTPException(404, "Research gap not found")
    conn.commit(); conn.close()
    return {"id": gap_id, "question": body.question.strip(), "status": "open"}


@app.post("/research/{mission_id}/gaps/{gap_id}/deepen")
async def deepen_research_gap(mission_id: str, gap_id: int):
    from behive.engine.deepening import create_run, ensure_schema
    ensure_schema()
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT gap_query FROM hive_gaps WHERE id=%s AND mission_id=%s", (gap_id, mission_id))
    row = cur.fetchone()
    if not row:
        conn.close(); raise HTTPException(404, "Research gap not found")
    cur.execute("SELECT id FROM hive_gap_runs WHERE gap_id=%s AND status IN ('queued','scouting','harvesting','extracting','evaluating') ORDER BY id DESC LIMIT 1", (gap_id,))
    active = cur.fetchone(); conn.close()
    if active:
        raise HTTPException(409, f"Gap already has active run {active[0]}")
    child_id = f"gap_{mission_id[-10:]}_{gap_id}_{int(time.time())}"
    run_id = create_run(gap_id, mission_id, child_id)
    asyncio.create_task(_run_gap_deepening(run_id, mission_id, gap_id, child_id, row[0]))
    return {"run_id": run_id, "gap_id": gap_id, "child_mission_id": child_id, "status": "queued"}


@app.post("/research/{mission_id}/gaps/{gap_id}/pause")
async def pause_research_gap(mission_id: str, gap_id: int):
    from behive.engine.deepening import ensure_schema, update_run
    ensure_schema(); conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM hive_gap_runs WHERE gap_id=%s AND parent_mission_id=%s AND status IN ('queued','scouting','harvesting','extracting','evaluating') ORDER BY id DESC LIMIT 1", (gap_id, mission_id))
    row = cur.fetchone(); conn.close()
    if not row: raise HTTPException(409, "No active gap run")
    proc = _gap_processes.get(row[0])
    if proc and proc.returncode is None:
        if os.name == "nt":
            import subprocess as _subprocess
            _subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.terminate()
    update_run(row[0], "paused", "Paused by user; checkpoint retained")
    return {"run_id": row[0], "status": "paused"}


@app.post("/research/{mission_id}/gaps/{gap_id}/dismiss")
async def dismiss_research_gap(mission_id: str, gap_id: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE hive_gaps SET resolved=TRUE WHERE id=%s AND mission_id=%s RETURNING id", (gap_id, mission_id))
    if not cur.fetchone(): conn.close(); raise HTTPException(404, "Research gap not found")
    conn.commit(); conn.close()
    return {"gap_id": gap_id, "status": "dismissed"}


@app.get("/research/{mission_id}/analysis/revisions")
async def get_analysis_revisions(mission_id: str):
    from behive.engine.deepening import ensure_schema
    ensure_schema(); conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id,gap_run_id,change_reason,created_at,previous_analysis,new_analysis FROM hive_analysis_revisions WHERE mission_id=%s ORDER BY id DESC LIMIT 50", (mission_id,))
    rows = cur.fetchall(); conn.close()
    return {"mission_id": mission_id, "revisions": [{"id": r[0], "gap_run_id": r[1], "reason": r[2],
            "created_at": r[3].isoformat() if r[3] else "", "previous_depth": (r[4] or {}).get("depth_status"),
            "new_depth": (r[5] or {}).get("depth_status"), "previous_claims": (r[4] or {}).get("evidence_metrics",{}).get("claims",0),
            "new_claims": (r[5] or {}).get("evidence_metrics",{}).get("claims",0)} for r in rows]}


@app.get("/claims/search")
async def search_claims(q: str, limit: int = Query(20, ge=1, le=100)):
    """Full-text search across all mission claims."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT claim, quality_score, source_url, mission_id, claim_type, confidence
            FROM hive_claims
            WHERE claim ILIKE %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC
            LIMIT %s
        """, (f"%{q}%", limit))
        results = [
            {"text": r[0], "quality_score": r[1], "source_url": r[2], "mission_id": r[3], "type": r[4], "confidence": r[5]}
            for r in cur.fetchall()
        ]
        conn.close()
        return {"query": q, "results": results, "total": len(results)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 8. Search alias ─────────────────────────────────────────────────────────

@app.get("/search")
async def search_alias(query: str = Query(..., alias="query"), limit: int = Query(20, ge=1, le=100)):
    """Alias for /claims/search."""
    return await search_claims(q=query, limit=limit)


# ─── 9. List Missions ────────────────────────────────────────────────────────

@app.get("/missions")
async def list_missions(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    """List all research missions, newest first."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.topic, m.status, m.phase, m.created_at,
                   COALESCE(c.cnt, 0) as claims_count,
                   COALESCE(c.avg_q, 0) as avg_quality
            FROM hive_missions m
            LEFT JOIN (
                SELECT mission_id, COUNT(*) as cnt, AVG(quality_score) as avg_q
                FROM hive_claims WHERE is_garbage = false OR is_garbage IS NULL
                GROUP BY mission_id
            ) c ON c.mission_id = m.id
            ORDER BY m.created_at DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        missions = [
            {
                "id": r[0], "topic": r[1], "status": r[2], "phase": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "claims_count": r[5], "avg_quality": round(float(r[6] or 0), 4),
            }
            for r in cur.fetchall()
        ]
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        total = cur.fetchone()[0]
        conn.close()
        return {"missions": missions, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 10. Intelligence: Entity ────────────────────────────────────────────────

@app.get("/intelligence/entity/{name}")
async def intelligence_entity(name: str, limit: int = Query(50, ge=1, le=200)):
    """Get intelligence about a specific entity — from knowledge graph or claims.
    
    Uses LLM-extracted typed entities from hive_entities (72K+ entries),
    with Neo4j graph as enrichment layer.
    """
    # Try Neo4j first (richest data)
    try:
        from behive.knowledge_graph import entity_lookup
        neo4j_result = entity_lookup(name, limit=limit)
        if neo4j_result:
            return neo4j_result
    except (ImportError, Exception):
        pass

    # Primary: hive_entities table (LLM-extracted, typed entities)
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Get entity occurrences with types
        cur.execute("""
            SELECT e.entity_type, e.value, e.context, e.confidence, e.mission_id
            FROM hive_entities e
            WHERE e.value ILIKE %s
            ORDER BY e.confidence DESC
            LIMIT %s
        """, (f"%{name}%", limit))
        entity_hits = cur.fetchall()
        
        # Get claims mentioning this entity
        cur.execute("""
            SELECT claim, quality_score, source_url, mission_id, claim_type, confidence
            FROM hive_claims
            WHERE claim ILIKE %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC
            LIMIT %s
        """, (f"%{name}%", limit))
        claims = [
            {"text": r[0], "quality_score": r[1], "source_url": r[2], 
             "mission_id": r[3], "type": r[4], "confidence": r[5]}
            for r in cur.fetchall()
        ]
        
        # Get co-occurring entities from hive_entities (same missions)
        mission_ids = list(set(r[4] for r in entity_hits))[:20]
        co_entities: Counter = Counter()
        if mission_ids:
            cur.execute("""
                SELECT value, COUNT(*) as cnt
                FROM hive_entities
                WHERE mission_id = ANY(%s)
                  AND value NOT ILIKE %s
                  AND entity_type NOT IN ('entitie', 'url', 'date')
                  AND LENGTH(value) > 2
                GROUP BY value
                ORDER BY cnt DESC
                LIMIT 30
            """, (mission_ids, f"%{name}%"))
            for row in cur.fetchall():
                co_entities[row[0]] = row[1]
        
        # Determine entity type from our data
        entity_types: Counter = Counter()
        for hit in entity_hits:
            if hit[0] and hit[0] != 'entitie':
                entity_types[hit[0]] += 1
        primary_type = entity_types.most_common(1)[0][0] if entity_types else "unknown"
        
        conn.close()

        top_related = [{"entity": e, "co_occurrences": c, "type": "co_occurrence"} 
                       for e, c in co_entities.most_common(20)]
        avg_q = sum(c["quality_score"] or 0 for c in claims) / max(1, len(claims))

        return {
            "entity": name,
            "entity_type": primary_type,
            "mentions": len(claims),
            "entity_records": len(entity_hits),
            "avg_quality": round(avg_q, 4),
            "related_entities": top_related,
            "claims": claims[:30],
            "source": "hive_entities+claims",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 11. Intelligence: Network ───────────────────────────────────────────────

@app.get("/intelligence/network/{name}")
async def intelligence_network(name: str, depth: int = Query(2, ge=1, le=3)):
    """Entity relationship network — from knowledge graph or hive_entities co-occurrence.
    
    Uses LLM-extracted typed entities (72K+ records) to build relationship networks.
    Edges represent co-occurrence within the same mission (semantically related).
    """
    # Try Neo4j first
    try:
        from behive.knowledge_graph import entity_network
        neo4j_result = entity_network(name, depth=depth)
        if neo4j_result:
            return neo4j_result
    except (ImportError, Exception):
        pass

    # Primary: hive_entities co-occurrence within missions
    try:
        conn = get_db()
        cur = conn.cursor()

        nodes = {}  # name -> {type, weight}
        edges = []
        nodes[name] = {"type": "center", "weight": 0}

        # Find missions containing this entity
        cur.execute("""
            SELECT DISTINCT mission_id FROM hive_entities
            WHERE value ILIKE %s
            LIMIT 30
        """, (f"%{name}%",))
        mission_ids = [r[0] for r in cur.fetchall()]
        nodes[name]["weight"] = len(mission_ids)

        if not mission_ids:
            conn.close()
            return {"center": name, "depth": depth, "nodes": [{"name": name, "type": "unknown", "weight": 0}], 
                    "edges": [], "node_count": 1, "edge_count": 0}

        # Hop 1: entities co-occurring in same missions (typed!)
        cur.execute("""
            SELECT value, entity_type, COUNT(DISTINCT mission_id) as cnt
            FROM hive_entities
            WHERE mission_id = ANY(%s)
              AND value NOT ILIKE %s
              AND entity_type NOT IN ('entitie', 'url', 'date')
              AND LENGTH(value) > 2
            GROUP BY value, entity_type
            ORDER BY cnt DESC
            LIMIT 20
        """, (mission_ids, f"%{name}%"))
        hop1_results = cur.fetchall()
        
        for entity_val, entity_type, weight in hop1_results:
            nodes[entity_val] = {"type": entity_type or "unknown", "weight": weight}
            edges.append({"source": name, "target": entity_val, "weight": weight, "relation": "co_occurs_in_research"})

        # Hop 2 (if depth >= 2): for top hop1 entities, find THEIR co-occurring entities
        if depth >= 2:
            for entity_val, entity_type, _ in hop1_results[:8]:
                cur.execute("""
                    SELECT DISTINCT mission_id FROM hive_entities
                    WHERE value ILIKE %s LIMIT 15
                """, (f"%{entity_val}%",))
                hop1_missions = [r[0] for r in cur.fetchall()]
                
                if hop1_missions:
                    cur.execute("""
                        SELECT value, entity_type, COUNT(DISTINCT mission_id) as cnt
                        FROM hive_entities
                        WHERE mission_id = ANY(%s)
                          AND value NOT ILIKE %s AND value NOT ILIKE %s
                          AND entity_type NOT IN ('entitie', 'url', 'date')
                          AND LENGTH(value) > 2
                        GROUP BY value, entity_type
                        ORDER BY cnt DESC
                        LIMIT 5
                    """, (hop1_missions, f"%{entity_val}%", f"%{name}%"))
                    for h2_val, h2_type, h2_weight in cur.fetchall():
                        if h2_val not in nodes:
                            nodes[h2_val] = {"type": h2_type or "unknown", "weight": h2_weight}
                        edges.append({"source": entity_val, "target": h2_val, "weight": h2_weight, "relation": "co_occurs_in_research"})

        conn.close()
        
        node_list = [{"name": n, "type": info["type"], "weight": info["weight"]} for n, info in nodes.items()]
        
        return {
            "center": name,
            "depth": depth,
            "nodes": node_list,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "source": "hive_entities",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 12. Intelligence: Stats ─────────────────────────────────────────────────

@app.get("/intelligence/stats")
async def intelligence_stats():
    """System-wide intelligence statistics including knowledge graph."""
    result = {}
    
    # Neo4j graph stats
    try:
        from behive.knowledge_graph import graph_stats
        gs = graph_stats()
        if gs:
            result["knowledge_graph"] = gs
    except ImportError:
        pass

    try:
        conn = get_db()
        cur = conn.cursor()

        # Missions
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        total_missions = cur.fetchone()[0]
        cur.execute("SELECT status, COUNT(*) FROM hive_missions GROUP BY status")
        missions_by_status = {r[0]: r[1] for r in cur.fetchall()}

        # Claims
        cur.execute("SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE is_garbage = false OR is_garbage IS NULL")
        row = cur.fetchone()
        total_claims, avg_quality = row[0], float(row[1] or 0)

        cur.execute("""
            SELECT claim_type, COUNT(*) FROM hive_claims
            WHERE is_garbage = false OR is_garbage IS NULL
            GROUP BY claim_type ORDER BY COUNT(*) DESC LIMIT 20
        """)
        claims_by_type = {r[0] or "unknown": r[1] for r in cur.fetchall()}

        # Top entities (sample recent claims, extract entities)
        cur.execute("""
            SELECT claim FROM hive_claims
            WHERE is_garbage = false OR is_garbage IS NULL
            ORDER BY quality_score DESC LIMIT 500
        """)
        entity_counter: Counter = Counter()
        for (claim_text,) in cur.fetchall():
            for e in extract_entities(claim_text):
                entity_counter[e] += 1

        top_entities = [{"entity": e, "mentions": c} for e, c in entity_counter.most_common(30)]

        # Quality distribution
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE quality_score >= 0.90) as excellent,
                COUNT(*) FILTER (WHERE quality_score >= 0.82 AND quality_score < 0.90) as great,
                COUNT(*) FILTER (WHERE quality_score >= 0.75 AND quality_score < 0.82) as good,
                COUNT(*) FILTER (WHERE quality_score >= 0.55 AND quality_score < 0.75) as acceptable,
                COUNT(*) FILTER (WHERE quality_score < 0.55) as rejected
            FROM hive_claims WHERE is_garbage = false OR is_garbage IS NULL
        """)
        qrow = cur.fetchone()
        quality_distribution = {
            "excellent_090+": qrow[0], "great_082+": qrow[1],
            "good_075+": qrow[2], "acceptable_055+": qrow[3], "rejected": qrow[4],
        }

        conn.close()
        result.update({
            "total_missions": total_missions,
            "total_claims": total_claims,
            "avg_quality": round(avg_quality, 4),
            "missions_by_status": missions_by_status,
            "claims_by_type": claims_by_type,
            "quality_distribution": quality_distribution,
            "top_entities": top_entities,
        })
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_pipeline(mission_id: str, topic: str, depth: int):
    """Run research pipeline as subprocess, stream progress via SSE events."""
    import sys
    
    # Concurrency limiter — max N missions at once
    sem = _get_semaphore()
    if sem.locked():
        _emit_event(mission_id, "queued", {"position": _MAX_CONCURRENT_MISSIONS, "message": "Waiting for slot..."})
        _update_phase(mission_id, "queued")
    
    async with sem:
        await _run_pipeline_inner(mission_id, topic, depth)


async def _run_recovery(mission_id: str, stage: str):
    """Run checkpoint recovery using the same interpreter and stream heartbeat."""
    import sys
    _mission_runtime[mission_id] = {"started_at": time.time(), "last_output_at": time.time(),
                                    "phase": stage, "returncode": None, "recovery": True}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "behive.engine", "resume", "--mission-id", mission_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "BEHIVE_MISSION_ID": mission_id, "PYTHONUNBUFFERED": "1"},
        )
        output_tail = []
        async for line in proc.stdout:
            _mission_runtime[mission_id]["last_output_at"] = time.time()
            rendered = line.decode(errors="replace").strip()
            if rendered:
                output_tail = (output_tail + [rendered])[-12:]
            lowered = rendered.lower()
            for detected in ("harvest", "process", "falsify", "synth", "graph"):
                if detected in lowered:
                    _mission_runtime[mission_id]["phase"] = detected
                    break
        await proc.wait()
        _mission_runtime[mission_id]["returncode"] = proc.returncode
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM hive_content WHERE mission_id=%s AND word_count>0", (mission_id,))
        content = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM hive_claims WHERE mission_id=%s", (mission_id,))
        claims = cur.fetchone()[0] or 0
        cur.execute("SELECT status,LENGTH(COALESCE(synthesis,'')) FROM hive_missions WHERE id=%s", (mission_id,))
        mission_row = cur.fetchone() or (None, 0)
        engine_status, synthesis_length = mission_row[0], mission_row[1] or 0
        if proc.returncode == 0 and engine_status == "insufficient":
            cur.execute(
                "UPDATE hive_missions SET quality_metrics=COALESCE(quality_metrics,'{}'::jsonb)||"
                "jsonb_build_object('recovery_outcome','insufficient_evidence','recovery_completed_at',NOW()) WHERE id=%s",
                (mission_id,),
            )
        elif proc.returncode == 0 and content > 0 and claims > 0:
            final_status = "done" if synthesis_length >= 100 else "partial"
            cur.execute("UPDATE hive_missions SET status=%s,phase=%s WHERE id=%s",
                        (final_status, final_status, mission_id))
            cur.execute(
                "UPDATE hive_missions SET quality_metrics=COALESCE(quality_metrics,'{}'::jsonb)||"
                "jsonb_build_object('recovery_outcome',%s,'recovery_completed_at',NOW()) WHERE id=%s",
                (final_status, mission_id),
            )
        else:
            detail = " | ".join(output_tail)[-1200:]
            cur.execute(
                "UPDATE hive_missions SET status='error',phase=%s,quality_metrics="
                "COALESCE(quality_metrics,'{}'::jsonb)||jsonb_build_object("
                "'failure_stage',%s,'error_message',%s,'error_detail',%s,'recoverable',TRUE) WHERE id=%s",
                (stage, stage, f"Recovery from {stage} failed: exit={proc.returncode}, content={content}, claims={claims}",
                 detail, mission_id),
            )
        conn.commit(); conn.close()
    except Exception as exc:
        _mission_runtime[mission_id]["error"] = str(exc)
        try:
            conn = get_db(); cur = conn.cursor()
            cur.execute("UPDATE hive_missions SET status='error',phase='error' WHERE id=%s", (mission_id,))
            conn.commit(); conn.close()
        except Exception:
            pass


async def _run_gap_deepening(run_id: int, parent_id: str, gap_id: int, child_id: str, question: str):
    """Run a narrow child mission, merge only qualified evidence, and selectively re-analyze."""
    import sys
    from behive.engine.deepening import merge_qualified_evidence, save_revision, update_run
    try:
        update_run(run_id, "scouting", "Searching specifically for primary and full-text evidence")
        targeted_question = (question + " Prioritize primary evidence: government and academic datasets, "
                             "peer-reviewed studies, official company filings, and original research reports. "
                             "Avoid news aggregators and search-result summaries.")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "behive.engine", "run", targeted_question,
            "--mission-id", child_id, "--depth", "4", "--scale", "40", "--force",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "BEHIVE_MISSION_ID": child_id, "PYTHONUNBUFFERED": "1",
                 "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                 "BEHIVE_GAP_RUN_ID": str(run_id), "BEHIVE_REQUIRE_PRIMARY": "1"},
        )
        _gap_processes[run_id] = proc
        output_tail = []
        async for raw in proc.stdout:
            rendered = raw.decode(errors="replace").strip()
            output_tail = (output_tail + [rendered])[-8:]
            text = rendered.lower()
            if "harvest" in text:
                update_run(run_id, "harvesting", "Retrieving candidate full-text sources")
            elif "process" in text or "extract" in text:
                update_run(run_id, "extracting", "Extracting claims from qualified evidence")
        await proc.wait()
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT status FROM hive_gap_runs WHERE id=%s", (run_id,))
        checkpoint_status = (cur.fetchone() or [None])[0]; conn.close()
        if checkpoint_status == "paused":
            return
        if proc.returncode != 0:
            detail = " | ".join(line for line in output_tail if line)[-450:]
            update_run(run_id, "failed", f"Deep research worker exited with code {proc.returncode}: {detail}")
            return
        update_run(run_id, "evaluating", "Applying full-text, independence, and primary-source gates")
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT analysis FROM hive_analysis WHERE mission_id=%s", (parent_id,))
        old_row = cur.fetchone(); previous = old_row[0] if old_row else None
        conn.close()
        metrics = merge_qualified_evidence(run_id)
        if metrics["merged_claims"] > 0:
            from behive.engine.analyst import analyze_mission
            current = await asyncio.to_thread(analyze_mission, parent_id)
            reason = (f"Gap {gap_id} added {metrics['merged_claims']} findings from "
                      f"{metrics['full_text_sources']} full-text sources ({metrics['primary_sources']} primary)")
            save_revision(parent_id, run_id, previous, current, reason)
        status = "resolved" if metrics["resolved"] else "exhausted"
        message = ("Evidence target reached; parent analysis revised" if metrics["resolved"] else
                   "Search completed but did not meet 3 full-text / 1 primary-source evidence gate")
        update_run(run_id, status, message, **metrics)
    except Exception as exc:
        update_run(run_id, "failed", str(exc)[:500])
    finally:
        _gap_processes.pop(run_id, None)


async def _run_pipeline_inner(mission_id: str, topic: str, depth: int):
    """Actual pipeline execution (called within semaphore)."""
    import sys

    try:
        _mission_runtime[mission_id] = {"started_at": time.time(), "last_output_at": time.time(),
                                        "phase": "starting", "returncode": None}
        _emit_event(mission_id, "start", {"topic": topic, "status": "scout"})

        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE hive_missions SET status = 'running', phase = 'scout' WHERE id = %s", (mission_id,))
        conn.commit()
        conn.close()

        _emit_event(mission_id, "phase", {"phase": "scout", "event": "started"})

        # Run orchestrator as subprocess with line-buffered output for SSE
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "behive.engine",
            "run", topic,
            "--mission-id", mission_id,
            "--depth", str(depth),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "BEHIVE_MISSION_ID": mission_id, "PYTHONUNBUFFERED": "1"},
        )

        # Parse subprocess output for phase transitions and progress
        current_phase = "scout"
        output_tail = []
        async for line in proc.stdout:
            text = line.decode(errors="replace").strip()
            if text:
                output_tail = (output_tail + [text])[-12:]
            _mission_runtime[mission_id]["last_output_at"] = time.time()
            # Detect phase transitions
            if "Scout done" in text or "scout.*done" in text.lower():
                current_phase = "harvest"
                _mission_runtime[mission_id]["phase"] = current_phase
                _emit_event(mission_id, "phase", {"phase": "harvest", "event": "started"})
                _update_phase(mission_id, "harvest")
            elif "Harvest" in text and "done" in text.lower():
                current_phase = "process"
                _mission_runtime[mission_id]["phase"] = current_phase
                _emit_event(mission_id, "phase", {"phase": "process", "event": "started"})
                _update_phase(mission_id, "process")
            elif "Process" in text and "done" in text.lower():
                current_phase = "synth"
                _mission_runtime[mission_id]["phase"] = current_phase
                _emit_event(mission_id, "phase", {"phase": "synth", "event": "started"})
                _update_phase(mission_id, "synth")
            # Detect claims progress
            elif "claims" in text.lower() and any(c.isdigit() for c in text):
                nums = re.findall(r'\d+', text)
                if nums:
                    _emit_event(mission_id, "claims", {"count": int(nums[0]), "phase": current_phase})

        await proc.wait()
        _mission_runtime[mission_id]["returncode"] = proc.returncode
        _mission_runtime[mission_id]["last_output_at"] = time.time()

        # Gather final results
        conn = get_db()
        cur = conn.cursor()
        if proc.returncode == 0:
            cur.execute("SELECT status FROM hive_missions WHERE id=%s", (mission_id,))
            engine_status = (cur.fetchone() or [None])[0]
            cur.execute(
                "SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)",
                (mission_id,)
            )
            row = cur.fetchone()
            if engine_status == "insufficient":
                _emit_event(mission_id, "done", {"total_claims": 0, "outcome": "insufficient_evidence"})
            elif engine_status in ("error", "interrupted", "cancelled") or not row[0]:
                cur.execute("UPDATE hive_missions SET status='error',phase='error' WHERE id=%s", (mission_id,))
                _emit_event(mission_id, "error", {"message": "Pipeline ended without usable findings"})
            else:
                cur.execute("UPDATE hive_missions SET status='done',phase='done' WHERE id=%s", (mission_id,))
                _emit_event(mission_id, "done", {"total_claims": row[0] or 0, "avg_quality": round(float(row[1] or 0), 4)})
        else:
            detail = " | ".join(output_tail)[-1200:]
            message = f"{current_phase.title()} worker exited with code {proc.returncode}"
            cur.execute(
                "UPDATE hive_missions SET status='error',phase=%s,quality_metrics="
                "COALESCE(quality_metrics,'{}'::jsonb)||jsonb_build_object("
                "'failure_stage',%s,'error_message',%s,'worker_exit_code',%s,'error_detail',%s,"
                "'recoverable',TRUE,'failed_at',NOW()) WHERE id=%s",
                (current_phase, current_phase, message, proc.returncode, detail, mission_id),
            )
            _emit_event(mission_id, "error", {"message": message, "stage": current_phase,
                                                 "recoverable": True, "detail": detail})
        conn.commit()
        conn.close()

    except Exception as e:
        _mission_runtime.setdefault(mission_id, {})["error"] = str(e)
        _mission_runtime[mission_id]["last_output_at"] = time.time()
        _emit_event(mission_id, "error", {"message": str(e)})
        try:
            conn = get_db()
            cur = conn.cursor()
            failure_stage = _mission_runtime.get(mission_id, {}).get("phase", "starting")
            cur.execute(
                "UPDATE hive_missions SET status='error',phase=%s,quality_metrics="
                "COALESCE(quality_metrics,'{}'::jsonb)||jsonb_build_object("
                "'failure_stage',%s,'error_message',%s,'recoverable',TRUE,'failed_at',NOW()) WHERE id=%s",
                (failure_stage, failure_stage, str(e)[:1000], mission_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


def _update_phase(mission_id: str, phase: str):
    """Update mission phase in DB."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE hive_missions SET phase = %s WHERE id = %s", (phase, mission_id))
        conn.commit()
        conn.close()
    except Exception:
        pass
