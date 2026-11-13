import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from agents_platform.check_in_api import router as check_in_router
from agents_platform.config import Settings, get_settings
from agents_platform.dashboard import router as dashboard_router
from agents_platform.db import Database
from agents_platform.memory_api import router as memory_router
from agents_platform.orchestration.client import connect_temporal
from agents_platform.orchestration.contracts import ProbeInput, ProbeResult
from agents_platform.orchestration.workflows import InfrastructureProbe
from agents_platform.supervisor_api import router as supervisor_router
from agents_platform.task_api import router as task_router

logger = logging.getLogger(__name__)


class ProbeRequest(BaseModel):
    delay_seconds: int = Field(default=1, ge=0, le=60)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=settings.log_level)
        app.state.settings = settings
        app.state.database = Database(settings.database_url)
        app.state.temporal = None
        # Clients connect lazily so liveness/docs remain available during an outage.
        app.state.temporal_lock = asyncio.Lock()
        try:
            yield
        finally:
            await app.state.database.close()

    app = FastAPI(title="HelloCounsel Agents Platform", version="0.1.0", lifespan=lifespan)
    app.include_router(task_router)
    app.include_router(check_in_router)
    app.include_router(memory_router)
    app.include_router(supervisor_router)
    app.include_router(dashboard_router)
    static = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(static / "index.html")

    async def temporal_client(request: Request):
        async with request.app.state.temporal_lock:
            if request.app.state.temporal is None:
                request.app.state.temporal = await connect_temporal(settings)
        return request.app.state.temporal

    @app.get("/health/live", tags=["health"])
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request):
        async def check_database():
            async with asyncio.timeout(5):
                await request.app.state.database.check()

        async def check_temporal():
            async with asyncio.timeout(5):
                client = await temporal_client(request)
                await client.workflow_service.describe_namespace(
                    DescribeNamespaceRequest(namespace=settings.temporal_namespace),
                    timeout=timedelta(seconds=3),
                )

        results = await asyncio.gather(check_database(), check_temporal(), return_exceptions=True)
        checks = {}
        for name, result in zip(("postgres", "temporal"), results, strict=True):
            checks[name] = "unavailable" if isinstance(result, BaseException) else "ok"
            if isinstance(result, BaseException):
                logger.warning("Readiness check failed for %s (%s)", name, type(result).__name__)
        healthy = all(value == "ok" for value in checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ready" if healthy else "not_ready", "checks": checks},
        )

    @app.post("/dev/probes", status_code=202, tags=["infrastructure"])
    async def start_probe(body: ProbeRequest, request: Request):
        probe_id = uuid4()
        try:
            async with asyncio.timeout(5):
                client = await temporal_client(request)
                await client.start_workflow(
                    InfrastructureProbe.run,
                    ProbeInput(delay_seconds=body.delay_seconds),
                    id=f"infra-probe-{probe_id}",
                    task_queue=settings.temporal_task_queue,
                    execution_timeout=timedelta(minutes=5),
                    rpc_timeout=timedelta(seconds=3),
                )
        except (RPCError, RuntimeError, TimeoutError) as exc:
            raise HTTPException(503, "Temporal unavailable; probe was not confirmed") from exc
        return {"probe_id": str(probe_id), "status_url": f"/dev/probes/{probe_id}"}

    @app.get("/dev/probes/{probe_id}", tags=["infrastructure"])
    async def get_probe(probe_id: UUID, request: Request):
        try:
            async with asyncio.timeout(5):
                client = await temporal_client(request)
                handle = client.get_workflow_handle(
                    f"infra-probe-{probe_id}", result_type=ProbeResult
                )
                description = await handle.describe(rpc_timeout=timedelta(seconds=3))
                response = {"probe_id": str(probe_id), "status": description.status.name}
                if description.status == WorkflowExecutionStatus.COMPLETED:
                    result = await handle.result(rpc_timeout=timedelta(seconds=3))
                    response["result"] = {
                        "postgres": result.postgres,
                        "pgvector_version": result.pgvector_version,
                    }
                return response
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(404, "Probe not found") from exc
            raise HTTPException(503, "Temporal unavailable") from exc
        except (RuntimeError, TimeoutError) as exc:
            raise HTTPException(503, "Temporal unavailable") from exc

    return app


app = create_app()
