from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from freightmesh_agent.models import Plan, Shipment, minutes_after_midnight
from freightmesh_agent.optimizer import optimize_world
from freightmesh_agent.simulation import LogisticsWorld
from freightmesh_agent.tools import (
    check_capacity,
    check_compatibility,
    check_route,
    check_sla,
    load_world,
)

_MAX_RECENT_RUNS = 100
_recent_runs: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT_RUNS)
_metrics_lock = threading.Lock()
_total_runs = 0
_blocked_requests = 0

app = FastAPI(
    title="FreightMesh Responsible AI Dashboard",
    description="Structured, deterministic logistics planning and evaluation API.",
    version="1.0.0",
)
app.mount(
    "/dashboard/assets",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "dashboard")),
    name="dashboard-assets",
)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    objective: Literal["cost", "carbon"] = "cost"
    amazon_deadline: str | None = None

    @field_validator("amazon_deadline")
    @classmethod
    def validate_deadline(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        try:
            minutes_after_midnight(value)
        except (ValueError, TypeError) as error:
            raise ValueError("deadline must be a valid 24-hour HH:MM value") from error
        if len(value) != 5 or value[2] != ":":
            raise ValueError("deadline must use HH:MM format")
        return value


def _record_blocked_request(category: str) -> None:
    global _blocked_requests
    with _metrics_lock:
        _blocked_requests += 1
        _recent_runs.appendleft({
            "run_id": f"BLOCK-{uuid4().hex[:12].upper()}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "blocked",
            "guardrail_category": category,
            "latency_ms": None,
            "token_usage": _zero_token_usage(),
        })


def _zero_token_usage() -> dict[str, Any]:
    return {
        "provider": "none",
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "measurement": "exact",
        "reason": "This structured API uses the deterministic optimizer and does not call an LLM.",
    }


def _data_source() -> dict[str, Any]:
    backend = os.getenv("FREIGHTMESH_DATA_BACKEND", "demo").lower()
    return {
        "backend": backend,
        "kind": "seeded simulation" if backend == "demo" else "configured Firestore records",
        "independently_verified": False,
    }


def _check(name: str, passed: bool, details: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "details": details}


def _evaluate_plan(
    world: LogisticsWorld,
    plan: Plan,
    rejections: tuple[str, ...],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    shipment_by_id = {shipment.shipment_id: shipment for shipment in world.shipments}
    allocation_ids = [allocation.shipment_id for allocation in plan.allocations]
    coverage_ok = (
        len(allocation_ids) == len(shipment_by_id)
        and len(set(allocation_ids)) == len(allocation_ids)
        and set(allocation_ids) == set(shipment_by_id)
    )
    checks.append(_check(
        "shipment_coverage",
        coverage_ok,
        f"{len(allocation_ids)} allocations for {len(shipment_by_id)} source shipments",
    ))

    grouped: dict[str, list[Shipment]] = {}
    truck_by_id = {truck.truck_id: truck for truck in world.trucks}
    shared_allocations: list[dict[str, Any]] = []
    dedicated_allocations: list[dict[str, Any]] = []
    for allocation in plan.allocations:
        shipment = shipment_by_id.get(allocation.shipment_id)
        if shipment is None:
            continue
        if allocation.truck_id is None:
            sla = check_sla(allocation.eta, (shipment,))
            checks.append(_check("sla", sla.feasible, sla.reason))
            dedicated_allocations.append({
                "shipment_id": shipment.shipment_id,
                "eta": allocation.eta,
                "cost_inr": allocation.cost,
                "emissions_kg": allocation.emissions_kg,
            })
        else:
            grouped.setdefault(allocation.truck_id, []).append(shipment)

    for truck_id, assigned in grouped.items():
        truck = truck_by_id.get(truck_id)
        if truck is None:
            checks.append(_check("truck_exists", False, f"{truck_id} is absent from source data"))
            continue
        shipments = tuple(assigned)
        shared_allocations.append({
            "truck_id": truck_id,
            "shipment_ids": [shipment.shipment_id for shipment in shipments],
            "eta": truck.eta,
            "cost_inr": truck.shared_cost(len(shipments)),
            "emissions_kg": truck.shared_emissions(len(shipments)),
            "usable_capacity_tons": truck.usable_capacity_tons,
            "allocated_weight_tons": sum(shipment.weight_tons for shipment in shipments),
        })
        for name, validation in (
            ("capacity", check_capacity(truck, shipments)),
            ("route", check_route(truck, shipments)),
            ("cargo_compatibility", check_compatibility(shipments)),
            ("sla", check_sla(truck.eta, shipments)),
        ):
            checks.append(_check(name, validation.feasible, validation.reason))

    reconciled_cost = (
        sum(item["cost_inr"] for item in dedicated_allocations)
        + sum(item["cost_inr"] for item in shared_allocations)
    )
    reconciled_emissions = (
        sum(item["emissions_kg"] for item in dedicated_allocations)
        + sum(item["emissions_kg"] for item in shared_allocations)
    )
    checks.append(_check(
        "cost_reconciliation",
        reconciled_cost == plan.cost,
        f"allocation totals={reconciled_cost} INR; plan total={plan.cost} INR",
    ))
    checks.append(_check(
        "emissions_reconciliation",
        abs(reconciled_emissions - plan.emissions_kg) < 0.001,
        f"allocation totals={reconciled_emissions:g} kg; plan total={plan.emissions_kg:g} kg",
    ))
    checks.append(_check(
        "savings_arithmetic",
        plan.savings == plan.baseline_cost - plan.cost,
        f"{plan.baseline_cost} - {plan.cost} = {plan.savings} INR",
    ))
    checks.append(_check(
        "nonnegative_emissions",
        plan.emissions_kg >= 0 and plan.baseline_emissions_kg >= 0,
        f"selected={plan.emissions_kg:g} kg; baseline={plan.baseline_emissions_kg:g} kg",
    ))

    savings_percent = (
        round(plan.savings / plan.baseline_cost * 100, 2)
        if plan.baseline_cost
        else None
    )
    emissions_reduction = plan.baseline_emissions_kg - plan.emissions_kg
    emissions_reduction_percent = (
        round(emissions_reduction / plan.baseline_emissions_kg * 100, 2)
        if plan.baseline_emissions_kg
        else None
    )
    return {
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "evaluation_method": "deterministic source-data and hard-constraint checks",
        "data_source": _data_source(),
        "source_shipments": [
            {
                "shipment_id": shipment.shipment_id,
                "company": shipment.company,
                "origin": shipment.origin,
                "destination": shipment.destination,
                "weight_tons": shipment.weight_tons,
                "deadline": shipment.deadline,
            }
            for shipment in world.shipments
        ],
        "selected_plan": {
            "objective": plan.objective,
            "baseline_cost_inr": plan.baseline_cost,
            "optimized_cost_inr": plan.cost,
            "savings_inr": plan.savings,
            "emissions_kg": plan.emissions_kg,
            "baseline_emissions_kg": plan.baseline_emissions_kg,
            "empty_km_saved": plan.empty_km_saved,
            "shipment_assignments": [
                {
                    "shipment_id": allocation.shipment_id,
                    "truck_id": allocation.truck_id,
                    "eta": allocation.eta,
                }
                for allocation in plan.allocations
            ],
            "dedicated_allocations": dedicated_allocations,
            "shared_truck_totals": shared_allocations,
        },
        "constraint_checks": checks,
        "rejected_candidates": list(rejections),
        "derived_metrics": [
            {
                "name": "savings_percent",
                "value": savings_percent,
                "formula": "(baseline_cost - optimized_cost) / baseline_cost * 100",
                "source": "selected_plan baseline_cost_inr and optimized_cost_inr",
            },
            {
                "name": "emissions_reduction_kg",
                "value": round(emissions_reduction, 2),
                "formula": "baseline_emissions_kg - emissions_kg",
                "source": "selected_plan baseline_emissions_kg and emissions_kg",
            },
            {
                "name": "emissions_reduction_percent",
                "value": emissions_reduction_percent,
                "formula": "(baseline_emissions_kg - emissions_kg) / baseline_emissions_kg * 100",
                "source": "selected_plan baseline_emissions_kg and emissions_kg",
            },
        ],
        "extrapolations": {
            "count": 0,
            "items": [],
            "note": "No future, population-wide, or live-performance values are extrapolated.",
        },
        "assumptions_and_limits": [
            "Source prices, ETAs, capacity, and emissions are treated as supplied facts, not independently verified measurements.",
            "The demo backend contains simulated fixture data; Firestore values are not independently audited.",
            "Emissions and empty-distance values are scenario inputs, not live sensor measurements.",
            "Passing these checks proves internal consistency with the configured data, not real-world accuracy.",
        ],
    }


def _execute(objective: Literal["cost", "carbon"], amazon_deadline: str | None) -> dict[str, Any]:
    world = load_world()
    plan, rejections = optimize_world(
        world,
        objective=objective,
        amazon_deadline=amazon_deadline,
    )
    evaluation_world = (
        world.with_deadline("AMZ-001", amazon_deadline)
        if amazon_deadline
        else world
    )
    return _evaluate_plan(evaluation_world, plan, rejections)


class ResponseHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith(("/run", "/evaluation", "/dashboard/api")):
            response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(ResponseHeadersMiddleware)


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del request
    del exc
    _record_blocked_request("unsupported_or_invalid_request")
    return JSONResponse(
        status_code=422,
        content={"detail": "Request rejected. Use only the documented fields and valid values."},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "dashboard", "index.html"),
        media_type="text/html",
    )


@app.get("/dashboard/api")
async def dashboard_data() -> dict[str, Any]:
    with _metrics_lock:
        recent = list(_recent_runs)
        total_runs = _total_runs
        blocked_requests = _blocked_requests
    completed = [item for item in recent if item.get("status") == "completed"]
    latencies = [
        item["latency_ms"]
        for item in completed
        if isinstance(item.get("latency_ms"), (int, float))
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics_scope": "process-local run history; resets when the API process restarts",
        "totals": {
            "completed_runs": total_runs,
            "blocked_requests": blocked_requests,
            "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "latency_sample_size": len(latencies),
        },
        "guardrails": {
            "status": "active",
            "blocked_requests": blocked_requests,
            "controls": [
                "Strict allow-listed objective and deadline fields",
                "Unknown request fields rejected without echoing values",
                "No free-text prompt is accepted by /run",
                "ADK inputs and model outputs are screened for common PII patterns",
                "Only approved objective/deadline fields are retained in recent run summaries",
            ],
            "limitations": [
                "Pattern screening cannot identify every kind of personal data or attack.",
                "The ADK Web development server persists conversation history; use this API dashboard for the privacy-minimized demo path.",
            ],
        },
        "token_usage": {
            **_zero_token_usage(),
            "aggregate_model_calls": 0,
            "aggregate_input_tokens": 0,
            "aggregate_output_tokens": 0,
            "scope": "The structured /run endpoint; ADK Web chat usage is not included.",
        },
        "recent_runs": recent[:20],
    }


@app.post("/run")
async def run_optimization(payload: RunRequest) -> dict[str, Any]:
    global _total_runs
    started = time.perf_counter()
    evaluation = _execute(payload.objective, payload.amazon_deadline)
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    run_id = f"RUN-{uuid4().hex[:12].upper()}"
    result = {
        "run_id": run_id,
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "deterministic_no_llm",
        "objective": payload.objective,
        "amazon_deadline": payload.amazon_deadline,
        "latency_ms": latency_ms,
        "token_usage": _zero_token_usage(),
        "guardrails": {
            "status": "passed",
            "raw_prompt_accepted": False,
            "raw_request_persisted": False,
            "human_approval_required": True,
        },
        "data_source": _data_source(),
        "plan": evaluation["selected_plan"],
        "sla_violations": sum(
            1 for check in evaluation["constraint_checks"]
            if check["name"] == "sla" and not check["passed"]
        ),
        "rejected_candidates": evaluation["rejected_candidates"],
        "evaluation": evaluation,
    }
    record = {
        "run_id": run_id,
        "timestamp": result["created_at"],
        "status": "completed",
        "objective": payload.objective,
        "amazon_deadline": payload.amazon_deadline,
        "evaluation_status": evaluation["status"],
        "latency_ms": latency_ms,
        "token_usage": _zero_token_usage(),
        "data_backend": result["data_source"]["backend"],
    }
    with _metrics_lock:
        _total_runs += 1
        _recent_runs.appendleft(record)
    return result


@app.get("/evaluation")
async def evaluation(
    objective: Literal["cost", "carbon"] = "cost",
    amazon_deadline: str | None = Query(
        default=None,
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
    ),
) -> dict[str, Any]:
    return _execute(objective, amazon_deadline or None)


def reset_dashboard_metrics() -> None:
    """Clear in-memory metric state for isolated tests."""
    global _total_runs, _blocked_requests
    with _metrics_lock:
        _recent_runs.clear()
        _total_runs = 0
        _blocked_requests = 0
