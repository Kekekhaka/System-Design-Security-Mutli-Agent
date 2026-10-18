import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.agents.multi_agent_graph import get_multi_agent_app, initial_state
from src.agents.nodes import is_approved
from src.core.config import settings
from src.core.observability import get_langfuse_callbacks
from src.core.security import get_current_user
from src.db.locks import RunInProgress, exclusive_user_run
from src.harness.optimizer import get_harness_optimizer, pick_winner

router = APIRouter(prefix="/project", tags=["project"])

MAX_REQUIREMENTS_CHARS = 20_000
# No ":" allowed, so "proj-{user_id}:{session_id}" always splits on its last
# colon and two users can never land on the same thread.
SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class ProjectRequest(BaseModel):
    requirements: str = Field(min_length=1, max_length=MAX_REQUIREMENTS_CHARS)
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class OptimizeRequest(BaseModel):
    requirements: str = Field(min_length=1, max_length=MAX_REQUIREMENTS_CHARS)


@router.post("/design")
async def design_project(
    req: ProjectRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict:
    app = getattr(request.app.state, "multi_agent_app", None) or get_multi_agent_app()
    config = {
        "configurable": {"thread_id": f"proj-{user_id}:{req.session_id}"},
        "callbacks": get_langfuse_callbacks(),
    }

    # A full critique-refine run can take minutes, past typical client and
    # proxy timeouts. Fail clearly and point to the streaming endpoint.
    try:
        result = await asyncio.wait_for(
            app.ainvoke(initial_state(req.requirements), config=config),
            timeout=settings.DESIGN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"Design run exceeded {settings.DESIGN_TIMEOUT_SECONDS}s; "
                "use POST /project/design/stream for long runs."
            ),
        )

    return {
        "status": "success",
        "approved": is_approved(result.get("security_findings")),
        "final_architecture": result.get("architecture"),
        "api_spec": result.get("api_spec"),
        "security_audit": result.get("security_findings"),
        "total_revisions": result.get("revision_count"),
    }


@router.post("/optimize")
async def optimize_project(
    req: OptimizeRequest, user_id: str = Depends(get_current_user)
) -> dict:
    # One run fans out to dozens of LLM calls; allow one in flight per user,
    # across all workers when Postgres is configured.
    try:
        async with exclusive_user_run("optimize", user_id):
            optimizer = get_harness_optimizer()
            result = await optimizer.ainvoke(
                {"task": req.requirements}, config={"callbacks": get_langfuse_callbacks()}
            )
    except RunInProgress:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="An optimization is already running for this user.",
        )
    winner = pick_winner(result["probe_results"])

    return {
        "status": "success",
        "winning_prompt": result["winning_prompt"],
        "approved": winner["approved"],
        "final_architecture": winner["architecture"],
        "api_spec": winner["api_spec"],
        "security_audit": winner["findings"],
        "total_revisions": winner["revisions"],
        "probes": [
            {
                "prompt": probe["prompt"],
                "revisions": probe["revisions"],
                "approved": probe["approved"],
            }
            for probe in result["probe_results"]
        ],
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/design/stream")
async def design_stream(
    req: ProjectRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    graph = getattr(request.app.state, "multi_agent_app", None) or get_multi_agent_app()
    config = {
        "configurable": {"thread_id": f"proj-{user_id}:{req.session_id}"},
        "callbacks": get_langfuse_callbacks(),
    }

    async def event_stream():
        final_state = None
        try:
            async for event in graph.astream_events(
                initial_state(req.requirements), config=config, version="v2"
            ):
                kind = event["event"]
                if kind == "on_chain_start" and event.get("name") in {
                    "supervisor",
                    "designer",
                    "security",
                }:
                    yield _sse("node", {"node": event["name"]})
                elif kind == "on_chat_model_stream":
                    chunk = event["data"].get("chunk")
                    text = getattr(chunk, "content", "") or ""
                    if text:
                        node = (event.get("metadata") or {}).get("langgraph_node", "")
                        yield _sse("token", {"node": node, "text": text})
                elif kind == "on_chain_end" and event.get("name") == "LangGraph":
                    output = event["data"].get("output")
                    if isinstance(output, dict):
                        final_state = output
        except Exception as exc:
            yield _sse("error", {"message": str(exc)})
            return

        if final_state is None:
            try:
                snapshot = await graph.aget_state(config)
                final_state = snapshot.values
            except Exception:
                final_state = None

        if final_state:
            yield _sse(
                "final",
                {
                    "approved": is_approved(final_state.get("security_findings")),
                    "final_architecture": final_state.get("architecture"),
                    "api_spec": final_state.get("api_spec"),
                    "security_audit": final_state.get("security_findings"),
                    "total_revisions": final_state.get("revision_count"),
                },
            )
        else:
            yield _sse("error", {"message": "stream ended without a final state"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
