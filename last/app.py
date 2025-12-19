from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Tuple

from flask import Flask, render_template, request
from rdflib import Graph, Namespace
from rdflib.namespace import RDF, RDFS

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
    applicable_type: str
    condition: str

@dataclass
class Task:
    iri: str
    label: str
    description: str
    applicable_type: str
    condition: str
    base_days: int
    per_item_days: Decimal
    per_fragile_days: Decimal
    intl_extra_days: int
    depends_on: List[str]

def _d(x) -> Decimal:
    try:
        if x is None:
            return Decimal("0")
        return Decimal(str(x))
    except (InvalidOperation, TypeError):
        return Decimal("0")

def load_graph() -> Tuple[Graph, Namespace]:
    g = Graph()
    g.parse(ONTOLOGY_PATH, format="turtle")
    ns = Namespace(NS_URI)
    return g, ns

def query_cities(g: Graph, NS: Namespace) -> Dict[str, City]:
    q = f"""
    SELECT ?city ?label ?code ?ccode WHERE {{
      ?city a <{NS.Город}> ;
            <{RDFS.label}> ?label ;
            <{NS.кодГорода}> ?code ;
            <{NS.странаКод}> ?ccode .
    }}
    """
    out: Dict[str, City] = {}
    for row in g.query(q):
        iri = str(row.city)
        out[iri] = City(iri=iri, label=str(row.label), code=str(row.code), country_code=str(row.ccode))
    return out

def query_services(g: Graph, NS: Namespace) -> Dict[str, Service]:
    q = f"""
    SELECT ?s ?name ?base ?per ?coef ?app ?cond WHERE {{
      ?s a <{NS.Услуга}> ;
         <{NS.названиеУслуги}> ?name ;
         <{NS.базоваяЦена}> ?base ;
         <{NS.ценаЗаПредмет}> ?per ;
         <{NS.коэффициентДляМеждународного}> ?coef ;
         <{NS.применимоКТипуУслуги}> ?app ;
         <{NS.условиеУслуги}> ?cond .
    }}
    """
    out: Dict[str, Service] = {}
    for row in g.query(q):
        iri = str(row.s)
        out[iri] = Service(
            iri=iri,
            name=str(row.name),
            base_price=_d(row.base),
            per_item_price=_d(row.per),
            intl_coef=_d(row.coef),
            applicable_type=str(row.app),
            condition=str(row.cond),
        )
    return out

def query_tasks(g: Graph, NS: Namespace) -> Dict[str, Task]:
    q = f"""
    SELECT ?t ?label ?desc ?app ?cond ?base ?pi ?pf ?intl WHERE {{
      ?t a <{NS.Задача}> ;
         <{RDFS.label}> ?label ;
         <{NS.описаниеЗадачи}> ?desc ;
         <{NS.применимоКТипу}> ?app ;
         <{NS.условиеЗадачи}> ?cond ;
         <{NS.днейБаза}> ?base ;
         <{NS.днейНаПредмет}> ?pi ;
         <{NS.днейНаХрупкий}> ?pf ;
         <{NS.днейЕслиМеждународный}> ?intl .
    }}
    """
    out: Dict[str, Task] = {}
    for row in g.query(q):
        iri = str(row.t)
        out[iri] = Task(
            iri=iri,
            label=str(row.label),
            description=str(row.desc),
            applicable_type=str(row.app),
            condition=str(row.cond),
            base_days=int(str(row.base)),
            per_item_days=_d(row.pi),
            per_fragile_days=_d(row.pf),
            intl_extra_days=int(str(row.intl)),
            depends_on=[],
        )

    q_dep = f"""
    SELECT ?t ?dep WHERE {{
      ?t a <{NS.Задача}> .
      OPTIONAL {{ ?t <{NS.зависитОт}> ?dep . }}
    }}
    """
    for row in g.query(q_dep):
        t = str(row.t)
        dep = str(row.dep) if row.dep is not None else None
        if dep and t in out:
            out[t].depends_on.append(dep)

    return out

def detect_move_type(destination: City) -> str:
    return "INTERNATIONAL" if destination.country_code != "RU" else "DOMESTIC_RU"

def parse_items_from_form() -> Tuple[List[dict], int, bool]:
    names = request.form.getlist("item_name")
    frag_flags = request.form.getlist("item_fragile")
    items: List[dict] = []
    fragile_count = 0
    for i, nm in enumerate(names):
        nm = (nm or "").strip()
        if not nm:
            continue
        is_frag = (i < len(frag_flags) and frag_flags[i] == "on")
        if is_frag:
            fragile_count += 1
        items.append({"name": nm, "fragile": is_frag})
    return items, fragile_count, fragile_count > 0

def select_services(services: Dict[str, Service], move_type: str) -> List[Service]:
    picked: List[Service] = []
    for s in services.values():
        if s.applicable_type not in ("ANY", move_type):
            continue
        if s.condition == "ANY":
            picked.append(s)
        elif s.condition == "Международный" and move_type == "INTERNATIONAL":
            picked.append(s)
    return picked

def estimate_cost(services: List[Service], n_items: int, move_type: str, employer_covers: bool) -> Tuple[Decimal, Decimal, Dict[str, Decimal]]:
    total = Decimal("0")
    breakdown: Dict[str, Decimal] = {}
    for s in services:
        cost = s.base_price + (s.per_item_price * Decimal(n_items))
        if move_type == "INTERNATIONAL":
            cost = cost * s.intl_coef
        cost = cost.quantize(Decimal("0.01"))
        breakdown[s.name] = cost
        total += cost
    out_of_pocket = total * (Decimal("0.30") if employer_covers else Decimal("1.00"))
    return total.quantize(Decimal("0.01")), out_of_pocket.quantize(Decimal("0.01")), breakdown

def select_tasks(tasks: Dict[str, Task], move_type: str, has_fragile: bool, has_work: bool, needs_permit: bool) -> List[Task]:
    picked: List[Task] = []
    for t in tasks.values():
        if t.applicable_type not in ("ANY", move_type):
            continue
        if t.condition == "ANY":
            picked.append(t)
        elif t.condition == "ХрупкоеЕсть" and has_fragile:
            picked.append(t)
        elif t.condition == "Международный" and move_type == "INTERNATIONAL":
            picked.append(t)
        elif t.condition == "НужноРазрешение" and move_type == "INTERNATIONAL" and has_work and needs_permit:
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
    queue = sorted([iri for iri, d in indeg.items() if d == 0])
    ordered: List[Task] = []
    while queue:
        iri = queue.pop(0)
        ordered.append(by_iri[iri])
        for nxt in adj[iri]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
                queue.sort()
    if len(ordered) != len(tasks):
        remaining = [by_iri[i] for i in by_iri if i not in {t.iri for t in ordered}]
        ordered.extend(remaining)
    return ordered

def ceil_decimal(d: Decimal) -> int:
    i = int(d)
    return i if d == Decimal(i) else i + 1

def task_duration_days(t: Task, n_items: int, fragile_count: int, move_type: str) -> int:
    d = Decimal(t.base_days)
    d += t.per_item_days * Decimal(n_items)
    d += t.per_fragile_days * Decimal(fragile_count)
    if move_type == "INTERNATIONAL":
        d += Decimal(t.intl_extra_days)
    return max(1, ceil_decimal(d))

def build_schedule_cp(ordered_tasks: List[Task], start: date, n_items: int, fragile_count: int, move_type: str) -> Tuple[List[dict], date, int, int]:
    by_iri = {t.iri: t for t in ordered_tasks}
    es: Dict[str, int] = {}
    ef: Dict[str, int] = {}
    dur: Dict[str, int] = {}
    for t in ordered_tasks:
        deps = [d for d in t.depends_on if d in by_iri]
        es_day = max((ef[d] for d in deps), default=0)
        d = task_duration_days(t, n_items, fragile_count, move_type)
        ef_day = es_day + d
        es[t.iri] = es_day
        ef[t.iri] = ef_day
        dur[t.iri] = d
    project_days = max(ef.values(), default=0)

    scale_buf = 0
    if n_items >= 40:
        scale_buf = 7
    elif n_items >= 25:
        scale_buf = 5
    elif n_items >= 15:
        scale_buf = 3

    fragile_buf = 0
    if fragile_count >= 15:
        fragile_buf = 3
    elif fragile_count >= 8:
        fragile_buf = 2
    elif fragile_count >= 4:
        fragile_buf = 1

    intl_buf = 10 if move_type == "INTERNATIONAL" else 2
    buffer_days = scale_buf + fragile_buf + intl_buf

    move_date = start + timedelta(days=project_days + buffer_days)

    plan = []
    for t in sorted(ordered_tasks, key=lambda x: es.get(x.iri, 0)):
        s = start + timedelta(days=es[t.iri])
        e = start + timedelta(days=ef[t.iri])
        plan.append({
            "task": t,
            "start": s,
            "end": e,
            "days": dur[t.iri],
            "deps": [by_iri[d].label for d in t.depends_on if d in by_iri],
        })

    return plan, move_date, project_days, buffer_days

app = Flask(__name__)

@app.get("/")
def index():
    g, NS = load_graph()
    cities = query_cities(g, NS)
    origin_city = next((c for c in cities.values() if c.code == "KRR" or c.label == "Краснодар"), None)
    dest_cities = [c for c in cities.values() if c != origin_city]
    dest_cities.sort(key=lambda x: x.label)
    return render_template("index.html", origin_city=origin_city, dest_cities=dest_cities, move_types=MOVE_TYPES)

@app.post("/recommend")
def recommend():
    g, NS = load_graph()

    cities = query_cities(g, NS)
    services = query_services(g, NS)
    tasks = query_tasks(g, NS)

    dest_iri = request.form.get("destination", "")
    destination = cities.get(dest_iri)

    origin = next((c for c in cities.values() if c.code == "KRR" or c.label == "Краснодар"), None)

    move_type_form = request.form.get("move_type", "DOMESTIC_RU")
    move_type_detected = detect_move_type(destination) if destination else move_type_form
    move_type = "INTERNATIONAL" if (move_type_form == "INTERNATIONAL" or move_type_detected == "INTERNATIONAL") else "DOMESTIC_RU"

    desired_budget_raw = request.form.get("desired_budget", "0")
    try:
        desired_budget = Decimal(desired_budget_raw).quantize(Decimal("0.01"))
    except Exception:
        desired_budget = Decimal("0.00")

    has_work = request.form.get("has_work") == "on"
    needs_permit = request.form.get("needs_permit") == "on"
    employer_covers = request.form.get("employer_covers") == "on"

    items, fragile_count, fragile_any = parse_items_from_form()
    n_items = len(items)
    if n_items == 0:
        try:
            n_items = int(request.form.get("items_count", "0") or "0")
        except Exception:
            n_items = 0

    picked_services = select_services(services, move_type)
    total_cost, out_of_pocket, breakdown = estimate_cost(picked_services, n_items, move_type, employer_covers)

    reserve = Decimal("1.25") if move_type == "INTERNATIONAL" else Decimal("1.15")
    recommended_budget = (out_of_pocket * reserve).quantize(Decimal("0.01"))
    delta = (desired_budget - recommended_budget).quantize(Decimal("0.01"))

    picked_tasks = select_tasks(tasks, move_type, fragile_any, has_work, needs_permit)
    ordered_tasks = topo_sort(picked_tasks)
    start_date = date.today()
    schedule, recommended_move_date, project_days, buffer_days = build_schedule_cp(
        ordered_tasks, start_date, n_items, fragile_count, move_type
    )

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
        fragile_count=fragile_count,
        items=items,
        fragile_any=fragile_any,
        services=picked_services,
        breakdown=breakdown,
        total_cost=total_cost,
        out_of_pocket=out_of_pocket,
        recommended_budget=recommended_budget,
        delta=delta,
        schedule=schedule,
        start_date=start_date,
        project_days=project_days,
        buffer_days=buffer_days,
        recommended_move_date=recommended_move_date,
        desired_move_date=desired_move_date,
        feasible=feasible,
    )

if __name__ == "__main__":
    app.run(debug=True)
