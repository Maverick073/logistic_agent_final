from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from freightmesh_agent.models import Allocation, ObjectiveWeights, Plan, Shipment, Truck, minutes_after_midnight
from freightmesh_agent.simulation import LogisticsWorld, primary_demo_world


@dataclass(frozen=True)
class Validation:
    feasible: bool
    reason: str


def check_capacity(truck: Truck, shipments: tuple[Shipment, ...]) -> Validation:
    load = sum(shipment.weight_tons for shipment in shipments)
    if load > truck.usable_capacity_tons:
        return Validation(
            False,
            f"capacity exceeded: {load:g} t requested, {truck.usable_capacity_tons:g} t usable",
        )
    return Validation(True, f"capacity OK: {load:g}/{truck.usable_capacity_tons:g} t")


def check_route(truck: Truck, shipments: tuple[Shipment, ...]) -> Validation:
    for shipment in shipments:
        if shipment.origin not in truck.route or shipment.destination not in truck.route:
            return Validation(False, f"{shipment.shipment_id} route is not covered by {truck.truck_id}")
        if truck.route.index(shipment.origin) >= truck.route.index(shipment.destination):
            return Validation(False, f"{shipment.shipment_id} route direction is incompatible")
    return Validation(True, "route compatible")


def check_compatibility(shipments: tuple[Shipment, ...]) -> Validation:
    unsupported = {"HAZARDOUS", "REFRIGERATED"}
    if any(shipment.cargo_type in unsupported for shipment in shipments):
        return Validation(False, "cargo requires unsupported special handling")
    return Validation(True, "cargo compatible")


def check_sla(eta: str, shipments: tuple[Shipment, ...]) -> Validation:
    late = [
        shipment.shipment_id
        for shipment in shipments
        if minutes_after_midnight(eta) > minutes_after_midnight(shipment.deadline)
    ]
    if late:
        return Validation(False, f"SLA missed for {', '.join(late)} (ETA {eta})")
    return Validation(True, f"SLA met at ETA {eta}")


def score_plan(
    *,
    cost: int,
    baseline_cost: int,
    emissions_kg: float,
    baseline_emissions_kg: float,
    utilization: float,
    empty_km_saved: float,
    baseline_empty_km: float,
    weights: ObjectiveWeights,
) -> float:
    cost_score = max(0.0, (baseline_cost - cost) / baseline_cost * 100)
    carbon_score = max(0.0, (baseline_emissions_kg - emissions_kg) / baseline_emissions_kg * 100)
    empty_score = min(100.0, empty_km_saved / baseline_empty_km * 100)
    return (
        cost_score * weights.cost
        + 100.0 * weights.sla
        + utilization * weights.utilization
        + empty_score * weights.empty_km
        + carbon_score * weights.carbon
    )


def build_plan(
    allocations: tuple[Allocation, ...],
    *,
    cost: int,
    baseline_cost: int,
    emissions_kg: float,
    baseline_emissions_kg: float,
    empty_km_saved: float,
    utilization: float,
    weights: ObjectiveWeights,
    objective: str,
) -> Plan:
    score = score_plan(
        cost=cost,
        baseline_cost=baseline_cost,
        emissions_kg=emissions_kg,
        baseline_emissions_kg=baseline_emissions_kg,
        utilization=utilization,
        empty_km_saved=empty_km_saved,
        baseline_empty_km=412,
        weights=weights,
    )
    return Plan(
        allocations=allocations,
        cost=cost,
        baseline_cost=baseline_cost,
        emissions_kg=emissions_kg,
        baseline_emissions_kg=baseline_emissions_kg,
        empty_km_saved=empty_km_saved,
        score=score,
        objective=objective,
    )


def _firestore_client() -> Any:
    from google.cloud import firestore

    return firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))


def load_world() -> LogisticsWorld:
    backend = os.getenv("FREIGHTMESH_DATA_BACKEND", "demo").lower()
    if backend == "demo":
        return primary_demo_world()
    if backend != "firestore":
        raise ValueError("FREIGHTMESH_DATA_BACKEND must be 'demo' or 'firestore'")

    client = _firestore_client()
    shipments = tuple(
        Shipment(**{**document.to_dict(), "shipment_id": document.id})
        for document in client.collection("shipments").stream()
    )
    trucks = tuple(
        Truck(
            **{
                **document.to_dict(),
                "truck_id": document.id,
                "route": tuple(document.to_dict()["route"]),
            }
        )
        for document in client.collection("trucks").stream()
    )
    if not shipments or not trucks:
        raise RuntimeError("Firestore must contain seeded shipments and trucks before optimizing.")
    return LogisticsWorld(shipments=shipments, trucks=trucks)


def _persist_plan(plan: Plan) -> str | None:
    if os.getenv("FREIGHTMESH_DATA_BACKEND", "demo").lower() != "firestore":
        return None
    run_id = f"RUN-{uuid4().hex[:12].upper()}"
    _firestore_client().collection("optimization_runs").document(run_id).set({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "objective": plan.objective,
        "cost": plan.cost,
        "baseline_cost": plan.baseline_cost,
        "savings": plan.savings,
        "emissions_kg": plan.emissions_kg,
        "baseline_emissions_kg": plan.baseline_emissions_kg,
        "empty_km_saved": plan.empty_km_saved,
        "allocations": [asdict(allocation) for allocation in plan.allocations],
        "approved": False,
    })
    return run_id


def _plan_result(plan: Plan, rejections: tuple[str, ...]) -> dict[str, Any]:
    return {
        "objective": plan.objective,
        "baseline_cost_inr": plan.baseline_cost,
        "optimized_cost_inr": plan.cost,
        "savings_inr": plan.savings,
        "emissions_kg": plan.emissions_kg,
        "baseline_emissions_kg": plan.baseline_emissions_kg,
        "empty_km_saved": plan.empty_km_saved,
        "sla_violations": 0,
        "score": round(plan.score, 2),
        "allocations": [asdict(allocation) for allocation in plan.allocations],
        "rejected_candidates": list(rejections),
        "human_approval_required": True,
    }


def _get_operator_report(company: str, shipment_ids: list[str]) -> dict[str, Any]:
    world = load_world()
    shipments = tuple(world.shipment(shipment_id) for shipment_id in shipment_ids)
    trucks = []
    for truck in world.trucks:
        if truck.company != company:
            continue
        if not all(shipment.origin in truck.route and shipment.destination in truck.route for shipment in shipments):
            continue
        load = sum(shipment.weight_tons for shipment in shipments)
        trucks.append({
            "truck_id": truck.truck_id,
            "available_tons": truck.usable_capacity_tons,
            "requested_tons": load,
            "capacity_feasible": load <= truck.usable_capacity_tons,
            "eta": truck.eta,
            "route": list(truck.route),
            "shareable": truck.shareable,
        })
    return {"company": company, "shipment_ids": shipment_ids, "truck_offers": trucks}


def get_blinkit_offer(shipment_ids: list[str]) -> dict[str, Any]:
    """Return Blinkit's route-matched capacity offers for the requested shipments."""
    return _get_operator_report("Blinkit", shipment_ids)


def get_shipment_record(shipment_id: str) -> dict[str, Any]:
    """Look up a shipment in the configured demo or Firestore world."""
    world = load_world()
    try:
        shipment = world.shipment(shipment_id)
    except KeyError:
        return {
            "found": False,
            "shipment_id": shipment_id,
            "source": os.getenv("FREIGHTMESH_DATA_BACKEND", "demo").lower(),
        }
    return {
        "found": True,
        "shipment": asdict(shipment),
        "source": os.getenv("FREIGHTMESH_DATA_BACKEND", "demo").lower(),
    }


def get_zepto_requirements() -> dict[str, Any]:
    """Return Zepto's demo shipment deadlines, cargo, and price cap."""
    world = load_world()
    return {
        "company": "Zepto",
        "max_acceptable_cost_inr": 15_000,
        "max_acceptable_delay_hours": 1,
        "shipments": [asdict(item) for item in world.shipments if item.company == "Zepto"],
    }


def get_amazon_requirements(amazon_deadline: str = "") -> dict[str, Any]:
    """Return Amazon's demo shipment deadlines and service preferences."""
    world = load_world()
    if amazon_deadline.strip():
        world = world.with_deadline("AMZ-001", amazon_deadline.strip())
    return {
        "company": "Amazon",
        "max_delay_hours": 2,
        "prefers_low_cost": True,
        "shipments": [asdict(item) for item in world.shipments if item.company == "Amazon"],
    }


def validate_consolidation(
    truck_id: str,
    shipment_ids: list[str],
    amazon_deadline: str = "",
) -> dict[str, Any]:
    """Check the requested load against vehicle, route, cargo, and SLA hard constraints."""
    world = load_world()
    if amazon_deadline.strip():
        world = world.with_deadline("AMZ-001", amazon_deadline.strip())
    truck = next((truck for truck in world.trucks if truck.truck_id == truck_id), None)
    if truck is None:
        raise ValueError(f"Unknown truck: {truck_id}")
    shipments = tuple(world.shipment(shipment_id) for shipment_id in shipment_ids)
    checks = (
        check_capacity(truck, shipments),
        check_route(truck, shipments),
        check_compatibility(shipments),
        check_sla(truck.eta, shipments),
    )
    return {
        "truck_id": truck_id,
        "shipment_ids": shipment_ids,
        "feasible": all(check.feasible for check in checks),
        "checks": [{"passed": check.feasible, "result": check.reason} for check in checks],
    }


def optimize_shipments(
    objective: str = "cost",
    amazon_deadline: str = "",
) -> dict[str, Any]:
    """Enumerate, validate, and rank feasible plans using exact demo costs and constraints."""
    from freightmesh_agent.optimizer import optimize_world

    deadline = amazon_deadline.strip() or None
    world = load_world()
    plan, rejections = optimize_world(world, objective=objective, amazon_deadline=deadline)
    result = _plan_result(plan, rejections)
    run_id = _persist_plan(plan)
    if run_id:
        result["firestore_optimization_run_id"] = run_id
    return result


def calculate_route(origin: str, destination: str) -> dict[str, Any]:
    """Calculate a route using Google Maps Routes API when configured; otherwise label the demo estimate."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        if os.getenv("FREIGHTMESH_REQUIRE_MAPS", "").lower() == "true":
            raise RuntimeError("GOOGLE_MAPS_API_KEY is required when FREIGHTMESH_REQUIRE_MAPS=true.")
        return {
            "provider": "simulated demo data",
            "origin": origin,
            "destination": destination,
            "distance_meters": 700_000,
            "duration": "8h",
            "note": "Seeded route estimate; configure GOOGLE_MAPS_API_KEY to use live Google Maps Routes API data.",
        }

    import requests

    response = requests.post(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.distanceMeters,routes.duration",
        },
        json={
            "origin": {"address": origin},
            "destination": {"address": destination},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
        },
        timeout=20,
    )
    response.raise_for_status()
    routes = response.json().get("routes", [])
    if not routes:
        raise RuntimeError(f"Google Maps Routes API returned no route for {origin} to {destination}.")
    return {
        "provider": "Google Maps Routes API",
        "origin": origin,
        "destination": destination,
        "distance_meters": routes[0]["distanceMeters"],
        "duration": routes[0]["duration"],
    }


def publish_logistics_event(
    event_type: str,
    shipment_id: str,
    deadline: str = "",
) -> dict[str, Any]:
    """Publish a shipment event to the configured Google Cloud Pub/Sub topic."""
    topic_id = os.getenv("FREIGHTMESH_PUBSUB_TOPIC")
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not topic_id or not project_id:
        raise RuntimeError(
            "Set GOOGLE_CLOUD_PROJECT and FREIGHTMESH_PUBSUB_TOPIC to publish Pub/Sub events."
        )
    if event_type not in {"NEW_SHIPMENT", "SHIPMENT_CANCELLED", "DEADLINE_CHANGED"}:
        raise ValueError("Unsupported shipment event type.")
    if not shipment_id.strip():
        raise ValueError("shipment_id must not be empty.")
    if event_type == "DEADLINE_CHANGED" and not deadline.strip():
        raise ValueError("deadline is required for DEADLINE_CHANGED events.")

    from google.cloud import pubsub_v1

    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(project_id, topic_id)
    event = {
        "event": event_type,
        "shipment_id": shipment_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    if deadline.strip():
        event["deadline"] = deadline.strip()
    payload = json.dumps(event).encode("utf-8")
    message_id = publisher.publish(topic_path, payload).result(timeout=30)
    return {"published": True, "topic": topic_path, "message_id": message_id}
