from flask import Flask, request
from rdflib import Graph, Namespace, RDF, RDFS
from rdflib.namespace import OWL
from datetime import datetime, date

app = Flask(__name__)

g = Graph()
g.parse("move_plan.owl", format="turtle")

EX = Namespace("http://example.com/move#")

label_map = {s: str(o) for s, p, o in g.triples((None, RDFS.label, None))}

task_classes = [
    EX.Task, EX.PackingTask, EX.BookingTask,
    EX.AdministrativeTask, EX.TransportationTask, EX.CleaningTask
]

@app.route("/")
def home():
    return """
    <html><head><meta charset="UTF-8"></head><body>
    <h1>Планирование переезда</h1>
    <ul>
      <li>
        <form action="/answer">
          Задачи до даты:
          <input type="date" name="date" required>
          <input type="hidden" name="q" value="1">
          <button>Показать</button>
        </form>
      </li>
      <li>
        <form action="/answer">
          <input type="hidden" name="q" value="2">
          <button>Ресурсы для упаковки</button>
        </form>
      </li>
      <li>
        <form action="/answer">
          <input type="hidden" name="q" value="3">
          <button>Оптимальный план переезда</button>
        </form>
      </li>
      <li>
        <form action="/answer">
          <input type="hidden" name="q" value="4">
          <button>Необходимые услуги</button>
        </form>
      </li>
      <li>
        <form action="/answer">
          <input type="hidden" name="q" value="5">
          <button>Предметы для перевозки</button>
        </form>
      </li>
      <li>
        <form action="/answer">
          <input type="hidden" name="q" value="6">
          <button>Показать классы и связи</button>
        </form>
      </li>
    </ul>
    </body></html>
    """

@app.route("/answer")
def answer():
    q = request.args.get("q")

    if q == "6":
        html = "<h2>Классы</h2><ul>"
        for c in g.subjects(RDF.type, OWL.Class):
            html += f"<li>{label_map.get(c, c)}</li>"
        html += "</ul><h2>Связи</h2><ul>"

        for p in g.subjects(RDF.type, OWL.ObjectProperty):
            d = [label_map.get(x, x) for x in g.objects(p, RDFS.domain)]
            r = [label_map.get(x, x) for x in g.objects(p, RDFS.range)]
            html += f"<li><b>{label_map.get(p, p)}</b>: {d} → {r}</li>"

        for p in g.subjects(RDF.type, OWL.DatatypeProperty):
            html += f"<li><b>{label_map.get(p, p)}</b> (datatype)</li>"

        return html + "</ul><a href='/'>Назад</a>"

    if q == "2":
        res = set()
        for t in g.subjects(RDF.type, EX.PackingTask):
            for r in g.objects(t, EX.requiresResource):
                res.add(label_map.get(r, r))
        return "<ul>" + "".join(f"<li>{x}</li>" for x in res) + "</ul><a href='/'>Назад</a>"

    if q == "5":
        items = g.objects(EX.MyMove, EX.hasItem)
        return "<ul>" + "".join(f"<li>{label_map.get(i, i)}</li>" for i in items) + "</ul><a href='/'>Назад</a>"

    if q == "4":
        services = set(g.objects(None, EX.requiresService))
        return "<ul>" + "".join(f"<li>{label_map.get(s, s)}</li>" for s in services) + "</ul><a href='/'>Назад</a>"

    if q == "1":
        cutoff = datetime.fromisoformat(request.args["date"]).date()
        tasks = []
        for t in g.subjects(RDF.type, None):
            if any((t, RDF.type, c) in g for c in task_classes):
                d = g.value(t, EX.dueDate)
                if d and d.toPython() <= cutoff:
                    tasks.append((d.toPython(), label_map.get(t, t)))
        tasks.sort()
        return "<ul>" + "".join(f"<li>{n} до {d}</li>" for d, n in tasks) + "</ul><a href='/'>Назад</a>"

    if q == "3":
        move = list(g.subjects(RDF.type, EX.TransportationTask))[0]
        d = g.value(move, EX.dueDate).toPython()
        return f"<p>Оптимальная дата переезда: <b>{d}</b></p><a href='/'>Назад</a>"

    return "Ошибка"

if __name__ == "__main__":
    app.run(debug=True)
