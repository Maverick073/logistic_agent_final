from __future__ import annotations

from collections import defaultdict
from itertools import product

from freightmesh_agent.models import Allocation, ObjectiveWeights, Plan, Shipment, minutes_after_midnight
from freightmesh_agent.simulation import LogisticsWorld
from freightmesh_agent.tools import (
    build_plan,
    check_capacity,
    check_compatibility,
    check_route,
    check_sla,
)


def optimize_world(
    world: LogisticsWorld,
    *,
    objective: str = "cost",
    amazon_deadline: str | None = None,
) -> tuple[Plan, tuple[str, ...]]:
    if objective not in {"cost", "carbon"}:
        raise ValueError("objective must be 'cost' or 'carbon'")
    if amazon_deadline is not None:
        world = world.with_deadline("AMZ-001", amazon_deadline)

    weights = ObjectiveWeights.carbon_first() if objective == "carbon" else ObjectiveWeights()
    shipments = world.shipments
    baseline_cost = sum(shipment.dedicated_cost for shipment in shipments)
    baseline_emissions = sum(shipment.dedicated_emissions_kg for shipment in shipments)
    options: list[tuple[str | None, ...]] = []
    for shipment in shipments:
        compatible_trucks = tuple(
            truck.truck_id
            for truck in world.trucks
            if shipment.origin in truck.route and shipment.destination in truck.route
        )
        options.append((None, *compatible_trucks))

    candidates: list[Plan] = []
    rejections: list[str] = []
    for choices in product(*options):
        grouped: dict[str, list[Shipment]] = defaultdict(list)
        for shipment, truck_id in zip(shipments, choices):
            if truck_id is not None:
                grouped[truck_id].append(shipment)

        allocations: list[Allocation] = []
        cost = 0
        emissions = 0.0
        empty_km_saved = 0.0
        utilization = 0.0
        invalid = False
        for truck_id, assigned in grouped.items():
            truck = next(truck for truck in world.trucks if truck.truck_id == truck_id)
            group = tuple(assigned)
            checks = (
                check_capacity(truck, group),
                check_route(truck, group),
                check_compatibility(group),
                check_sla(truck.eta, group),
            )
            failures = tuple(check.reason for check in checks if not check.feasible)
            if failures:
                if len(group) == len(shipments):
                    rejections.append(f"{truck_id}: {'; '.join(failures)}")
                invalid = True
                break
            if not truck.shareable:
                rejections.append(f"{truck_id}: company policy does not allow sharing")
                invalid = True
                break

            cost += truck.shared_cost(len(group))
            emissions += truck.shared_emissions(len(group))
            empty_km_saved = max(empty_km_saved, truck.empty_km_saved)
            utilization += sum(shipment.weight_tons for shipment in group) / truck.capacity_tons * 100
            for shipment in group:
                allocations.append(Allocation(
                    shipment_id=shipment.shipment_id,
                    truck_id=truck.truck_id,
                    eta=truck.eta,
                    cost=0,
                    emissions_kg=0,
                ))

        if invalid:
            continue

        for shipment, truck_id in zip(shipments, choices):
            if truck_id is not None:
                continue
            if minutes_after_midnight(shipment.dedicated_eta) > minutes_after_midnight(shipment.deadline):
                rejections.append(
                    f"{shipment.shipment_id}: dedicated ETA {shipment.dedicated_eta} misses "
                    f"the {shipment.deadline} SLA"
                )
                invalid = True
                break
            cost += shipment.dedicated_cost
            emissions += shipment.dedicated_emissions_kg
            allocations.append(Allocation(
                shipment_id=shipment.shipment_id,
                truck_id=None,
                eta=shipment.dedicated_eta,
                cost=shipment.dedicated_cost,
                emissions_kg=shipment.dedicated_emissions_kg,
            ))
        if invalid or not allocations:
            continue

        candidates.append(build_plan(
            tuple(allocations),
            cost=cost,
            baseline_cost=baseline_cost,
            emissions_kg=emissions,
            baseline_emissions_kg=baseline_emissions,
            empty_km_saved=empty_km_saved,
            utilization=utilization,
            weights=weights,
            objective="carbon-aware" if objective == "carbon" else "cost",
        ))

    if not candidates:
        raise RuntimeError("No feasible transportation plan was found.")
    if objective == "cost":
        selected = min(candidates, key=lambda plan: (plan.cost, -plan.score))
    else:
        selected = max(candidates, key=lambda plan: (plan.score, -plan.cost))
    return selected, tuple(dict.fromkeys(rejections))
