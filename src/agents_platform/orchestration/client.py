from temporalio.client import Client

from agents_platform.config import Settings
from agents_platform.observability import runtime, tracer


async def connect_temporal(settings: Settings) -> Client:
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        interceptors=tracing_interceptors(),
    )


def tracing_interceptors():
    if not runtime().enabled:
        return []
    from temporalio.contrib.opentelemetry import TracingInterceptor

    return [TracingInterceptor(tracer=tracer(), always_create_workflow_spans=True)]
