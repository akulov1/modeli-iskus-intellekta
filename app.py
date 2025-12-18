from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

from flask import Flask, render_template, request

try:
    from rdflib import Graph, Namespace
    from rdflib.namespace import RDF, RDFS, XSD
    RDFLIB_AVAILABLE = True
except Exception:
    RDFLIB_AVAILABLE = False


APP_DIR = os.path.dirname(os.path.abspath(__file__))
ONTOLOGY_PATH = os.path.join(APP_DIR, "ontology.ttl")

NS_URI = "http://example.org/ontologies/planirovanie-pereezda#"

MOVE_TYPES = {
    "DOMESTIC_RU": "Переезд по России",
    "INTERNATIONAL": "Переезд за границу",
}


@dataclass
class City:
    iri: str
    label: str
    code: str
    country_code: str


@dataclass
class Service:
    iri: str
    name: str
    base_price: Decimal
    per_item_price: Decimal
    intl_coef: Decimal


@dataclass
class Task:
    iri: str
    label: str
    description: str
    days: int
    applicable_type: str
    condition: str
    depends_on: List[str]


def _d(s: str) -> Decimal:
    try:
        return Decimal(s)
    except (InvalidOperation, TypeError):
        return Decimal("0")


def load_ontology() -> Tuple[Dict[str, City], Dict[str, Service], Dict[str, Task]]:
    if not RDFLIB_AVAILABLE:
        return fallback_data()

    g = Graph()
    g.parse(ONTOLOGY_PATH, format="turtle")
    NS = Namespace(NS_URI)

    cities: Dict[str, City] = {}
    for s in g.subjects(RDF.type, NS.Город):
        label = str(g.value(s, RDFS.label) or "")
        code = str(g.value(s, NS.кодГорода) or "")
        ccode = str(g.value(s, NS.странаКод) or "")
        cities[str(s)] = City(iri=str(s), label=label, code=code, country_code=ccode)

    services: Dict[str, Service] = {}
    for s in g.subjects(RDF.type, NS.Услуга):
        name = str(g.value(s, NS.названиеУслуги) or g.value(s, RDFS.label) or "")
        base_price = _d(str(g.value(s, NS.базоваяЦена) or "0"))
        per_item = _d(str(g.value(s, NS.ценаЗаПредмет) or "0"))
        coef = _d(str(g.value(s, NS.коэффициентДляМеждународного) or "1"))
        services[str(s)] = Service(iri=str(s), name=name, base_price=base_price, per_item_price=per_item, intl_coef=coef)

    tasks: Dict[str, Task] = {}
    for s in g.subjects(RDF.type, NS.Задача):
        label = str(g.value(s, RDFS.label) or "")
        desc = str(g.value(s, NS.описаниеЗадачи) or "")
        days_val = g.value(s, NS.ориентирДней)
        days = int(str(days_val)) if days_val is not None else 1
        applicable = str(g.value(s, NS.применимоКТипу) or "ANY")
        cond = str(g.value(s, NS.условиеЗадачи) or "ANY")
        depends = [str(o) for o in g.objects(s, NS.зависитОт)]
        tasks[str(s)] = Task(
            iri=str(s),
            label=label,
            description=desc,
            days=days,
            applicable_type=applicable,
            condition=cond,
            depends_on=depends,
        )

    return cities, services, tasks


def fallback_data() -> Tuple[Dict[str, City], Dict[str, Service], Dict[str, Task]]:
    cities = {
        "KRR": City("KRR", "Краснодар", "KRR", "RU"),
        "MOW": City("MOW", "Москва", "MOW", "RU"),
        "LED": City("LED", "Санкт-Петербург", "LED", "RU"),
        "BER": City("BER", "Берлин", "BER", "DE"),
    }
    services = {
        "move": Service("move", "Перевозка", Decimal("12000"), Decimal("650"), Decimal("2.0")),
        "pack": Service("pack", "Упаковка", Decimal("2500"), Decimal("140"), Decimal("1.15")),
        "docs": Service("docs", "Документы/переводы", Decimal("9000"), Decimal("0"), Decimal("1.30")),
    }
    tasks = {
        "t1": Task("t1", "Составить список вещей", "Добавить все предметы для расчёта стоимости.", 1, "ANY", "ANY", []),
        "t2": Task("t2", "Купить упаковку", "Купить упаковку пропорционально количеству предметов.", 1, "ANY", "ANY", ["t1"]),
        "t3": Task("t3", "Упаковать хрупкое", "Упаковать хрупкие предметы.", 2, "ANY", "ХрупкоеЕсть", ["t2"]),
        "t4": Task("t4", "Заказать перевозку", "Заказать перевозку с учётом количества предметов.", 1, "ANY", "ANY", ["t1"]),
        "t5": Task("t5", "Проверить визовые требования", "Подготовить документы для визы/ВНЖ.", 7, "INTERNATIONAL", "Международный", ["t1"]),
    }
    return cities, services, tasks


def parse_items_from_form() -> Tuple[List[dict], bool]:
    names = request.form.getlist("item_name")
    volumes = request.form.getlist("item_volume")
    fragiles = request.form.getlist("item_fragile")
    items = []
    fragile_any = False

    fragile_idx = set()
    for v in request.form.getlist("item_fragile_idx"):
        try:
            fragile_idx.add(int(v))
        except Exception:
            pass

    for i, nm in enumerate(names):
        nm = (nm or "").strip()
        if not nm:
            continue
        vol = volumes[i] if i < len(volumes) else ""
        try:
            vol_d = float(vol) if str(vol).strip() else 0.0
        except Exception:
            vol_d = 0.0
        is_frag = i in fragile_idx
        fragile_any = fragile_any or is_frag
        items.append({"name": nm, "volume": vol_d, "fragile": is_frag})

    return items, fragile_any


def select_services(services: Dict[str, Service], move_type: str) -> List[Service]:
    result = []
    for s in services.values():
        if s.name.lower().startswith("перевоз") or s.name.lower().startswith("упаков"):
            result.append(s)
        if move_type == "INTERNATIONAL" and "документ" in s.name.lower():
            result.append(s)
    uniq = {}
    for s in result:
        uniq[s.iri] = s
    return list(uniq.values())


def estimate_cost(services: List[Service], n_items: int, move_type: str, employer_covers: bool) -> Tuple[Decimal, Decimal]:
    total = Decimal("0")
    for s in services:
        cost = s.base_price + (s.per_item_price * Decimal(n_items))
        if move_type == "INTERNATIONAL":
            cost = cost * s.intl_coef
        total += cost

    out_of_pocket = total * (Decimal("0.30") if employer_covers else Decimal("1.00"))
    return total.quantize(Decimal("0.01")), out_of_pocket.quantize(Decimal("0.01"))


def select_tasks(tasks: Dict[str, Task], move_type: str, has_fragile: bool) -> List[Task]:
    picked = []
    for t in tasks.values():
        if t.applicable_type not in ("ANY", move_type):
            continue
        if t.condition == "ANY":
            picked.append(t)
        elif t.condition == "ХрупкоеЕсть" and has_fragile:
            picked.append(t)
        elif t.condition == "Международный" and move_type == "INTERNATIONAL":
            picked.append(t)
    return picked


def topo_sort(tasks: List[Task]) -> List[Task]:
    by_iri = {t.iri: t for t in tasks}
    indeg = {t.iri: 0 for t in tasks}
    adj = {t.iri: [] for t in tasks}

    for t in tasks:
        for dep in t.depends_on:
            if dep in by_iri:
                indeg[t.iri] += 1
                adj[dep].append(t.iri)

    queue = [iri for iri, d in indeg.items() if d == 0]
    ordered = []

    while queue:
        iri = queue.pop(0)
        ordered.append(by_iri[iri])
        for nxt in adj[iri]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)

    if len(ordered) != len(tasks):
        remaining = [by_iri[i] for i in by_iri.keys() if i not in {t.iri for t in ordered}]
        ordered.extend(remaining)

    return ordered


def build_schedule(ordered_tasks: List[Task], start: date) -> Tuple[List[dict], date]:
    plan = []
    cur = start
    for t in ordered_tasks:
        start_t = cur
        end_t = cur + timedelta(days=max(1, int(t.days)))
        plan.append({
            "task": t,
            "start": start_t,
            "end": end_t,
        })
        cur = end_t

    total_days = sum(max(1, int(t.days)) for t in ordered_tasks)
    buffer_days = max(1, int(round(total_days * 0.10)))
    move_date = cur + timedelta(days=buffer_days)
    return plan, move_date


app = Flask(__name__)


@app.get("/")
def index():
    cities, services, tasks = load_ontology()
    origin_code = "KRR"
    origin_city = None
    dest_cities: List[City] = []

    for c in cities.values():
        if c.code == origin_code:
            origin_city = c
        else:
            dest_cities.append(c)

    dest_cities.sort(key=lambda x: x.label)
    return render_template(
        "index.html",
        origin_city=origin_city,
        dest_cities=dest_cities,
        move_types=MOVE_TYPES
    )


@app.post("/recommend")
def recommend():
    cities, services, tasks = load_ontology()

    move_type = request.form.get("move_type", "DOMESTIC_RU")
    dest_iri_or_code = request.form.get("destination", "")

    origin = next((c for c in cities.values() if c.code == "KRR" or c.label.startswith("Краснодар")), None)

    destination = cities.get(dest_iri_or_code)
    if destination is None:
        destination = next((c for c in cities.values() if c.code == dest_iri_or_code), None)

    desired_budget_raw = request.form.get("desired_budget", "0")
    try:
        desired_budget = Decimal(desired_budget_raw).quantize(Decimal("0.01"))
    except Exception:
        desired_budget = Decimal("0.00")

    has_work = request.form.get("has_work") == "on"
    needs_permit = request.form.get("needs_permit") == "on"
    employer_covers = request.form.get("employer_covers") == "on"

    # Items (variable count)
    items, fragile_any = parse_items_from_form()
    n_items = len(items)

    if n_items == 0:
        try:
            n_items = int(request.form.get("items_count", "0") or "0")
        except Exception:
            n_items = 0

    picked_services = select_services(services, move_type)
    total_cost, out_of_pocket = estimate_cost(picked_services, n_items, move_type, employer_covers)

    recommended_budget = (out_of_pocket * Decimal("1.15")).quantize(Decimal("0.01"))
    delta = (desired_budget - recommended_budget).quantize(Decimal("0.01"))

    picked_tasks = select_tasks(tasks, move_type, fragile_any)
    ordered_tasks = topo_sort(picked_tasks)
    start_date = date.today()
    schedule, recommended_move_date = build_schedule(ordered_tasks, start_date)

    desired_move_date_str = (request.form.get("desired_move_date") or "").strip()
    desired_move_date = None
    feasible = None
    if desired_move_date_str:
        try:
            desired_move_date = date.fromisoformat(desired_move_date_str)
            feasible = desired_move_date >= recommended_move_date
        except Exception:
            desired_move_date = None
            feasible = None

    return render_template(
        "result.html",
        origin=origin,
        destination=destination,
        move_type=move_type,
        move_type_label=MOVE_TYPES.get(move_type, move_type),
        desired_budget=desired_budget,
        has_work=has_work,
        needs_permit=needs_permit,
        employer_covers=employer_covers,
        n_items=n_items,
        items=items,
        fragile_any=fragile_any,
        services=picked_services,
        total_cost=total_cost,
        out_of_pocket=out_of_pocket,
        recommended_budget=recommended_budget,
        delta=delta,
        schedule=schedule,
        start_date=start_date,
        recommended_move_date=recommended_move_date,
        desired_move_date=desired_move_date,
        feasible=feasible,
    )


if __name__ == "__main__":
    app.run(debug=True)
