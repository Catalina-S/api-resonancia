from datetime import datetime
from io import BytesIO
from typing import Optional
from uuid import uuid4

import numpy as np
import pydicom
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from PIL import Image
from pydantic import BaseModel, Field

app = FastAPI(title="API FHIR de imágenes por resonancia", version="3.0.0")

DCM = "http://dicom.nema.org/resources/ontology/DCM"
ALLOWED_TYPES = {"application/dicom", "image/png"}

patients: dict[str, dict] = {}
devices: dict[str, dict] = {}
studies: dict[str, dict] = {}
binaries: dict[str, dict] = {}


def outcome(status: int, code: str, msg: str):
    raise HTTPException(
        status_code=status,
        detail={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code, "diagnostics": msg}],
        },
    )


def bundle(resources: list[dict]) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


def ref_id(reference: str, tipo: str) -> str:
    if not reference.startswith(tipo + "/"):
        outcome(400, "invalid", f"Referencia inválida: {reference}")
    return reference.split("/", 1)[1]


# ---------------- Patient ----------------

class PatientIn(BaseModel):
    family: str = Field(..., examples=["Gómez"])
    given: str = Field(..., examples=["Laura"])
    gender: str = Field("unknown", pattern="^(male|female|other|unknown)$")
    birthDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", examples=["1985-04-12"])
    identifier: str = Field(..., examples=["HC-100245"])


def patient_resource(pid: str, p: PatientIn) -> dict:
    return {
        "resourceType": "Patient",
        "id": pid,
        "active": True,
        "identifier": [{"system": "urn:sim:historia-clinica", "value": p.identifier}],
        "name": [{"family": p.family, "given": [p.given]}],
        "gender": p.gender,
        "birthDate": p.birthDate,
    }


@app.post("/fhir/Patient", status_code=201, tags=["1. Pacientes"])
def create_patient(body: PatientIn, response: Response):
    pid = str(uuid4())
    patients[pid] = patient_resource(pid, body)
    response.headers["Location"] = f"/fhir/Patient/{pid}"
    return patients[pid]


@app.get("/fhir/Patient", tags=["1. Pacientes"])
def search_patients(family: Optional[str] = None):
    items = [
        p for p in patients.values()
        if family is None or p["name"][0]["family"].lower() == family.lower()
    ]
    return bundle(items)


@app.get("/fhir/Patient/{pid}", tags=["1. Pacientes"])
def get_patient(pid: str):
    if pid not in patients:
        outcome(404, "not-found", f"Patient/{pid} no existe")
    return patients[pid]


# ---------------- Device ----------------

class DeviceIn(BaseModel):
    status: str = Field("active", pattern="^(active|inactive|entered-in-error|unknown)$")
    typeText: str = Field(..., examples=["Resonancia magnética 3T"])
    manufacturer: str = Field(..., examples=["Siemens"])
    modelName: str = Field(..., examples=["MAGNETOM Skyra"])
    serialNumber: Optional[str] = None


def device_resource(did: str, d: DeviceIn) -> dict:
    r = {
        "resourceType": "Device",
        "id": did,
        "status": d.status,
        "type": {"text": d.typeText},
        "manufacturer": d.manufacturer,
        "deviceName": [{"name": d.modelName, "type": "model-name"}],
    }
    if d.serialNumber:
        r["serialNumber"] = d.serialNumber
    return r


@app.post("/fhir/Device", status_code=201, tags=["2. Dispositivos (equipos de RM)"])
def create_device(body: DeviceIn, response: Response):
    did = str(uuid4())
    devices[did] = device_resource(did, body)
    response.headers["Location"] = f"/fhir/Device/{did}"
    return devices[did]


@app.get("/fhir/Device", tags=["2. Dispositivos (equipos de RM)"])
def search_devices():
    return bundle(list(devices.values()))


@app.get("/fhir/Device/{did}", tags=["2. Dispositivos (equipos de RM)"])
def get_device(did: str):
    if did not in devices:
        outcome(404, "not-found", f"Device/{did} no existe")
    return devices[did]


# ---------------- ImagingStudy ----------------

class SeriesIn(BaseModel):
    number: int = Field(..., ge=1)
    modality: str = Field("MR", examples=["MR"])
    description: str = Field(..., examples=["T1 axial"])
    deviceId: Optional[str] = None


class ImagingStudyIn(BaseModel):
    patientRef: str = Field(..., examples=["Patient/ID-DEL-PACIENTE"])
    started: datetime
    description: str = ""
    series: list[SeriesIn] = Field(..., min_length=1)


def render_study(sid: str) -> dict:
    s = studies[sid]
    p = patients[ref_id(s["patientRef"], "Patient")]
    nombre = f"{p['name'][0]['given'][0]} {p['name'][0]['family']}"
    inst = [b for b in binaries.values() if b["studyId"] == sid]

    series_out = []
    for sr in s["series"]:
        item = {
            "uid": f"{s['uid']}.{sr['number']}",
            "number": sr["number"],
            "modality": {"system": DCM, "code": sr["modality"]},
            "description": sr["description"],
            "numberOfInstances": sum(1 for b in inst if b["series"] == sr["number"]),
        }
        if sr["deviceId"]:
            d = devices[sr["deviceId"]]
            item["performer"] = [{
                "actor": {
                    "reference": f"Device/{d['id']}",
                    "display": f"{d['manufacturer']} {d['deviceName'][0]['name']}",
                }
            }]
        series_out.append(item)

    return {
        "resourceType": "ImagingStudy",
        "id": sid,
        "status": "available" if inst else "registered",
        "subject": {"reference": s["patientRef"], "display": nombre},
        "started": s["started"],
        "description": s["description"],
        "identifier": [{"system": "urn:dicom:uid", "value": f"urn:oid:{s['uid']}"}],
        "modality": [{"system": DCM, "code": "MR"}],
        "numberOfSeries": len(series_out),
        "numberOfInstances": len(inst),
        "series": series_out,
    }


@app.post("/fhir/ImagingStudy", status_code=201, tags=["3. Estudios de RM"])
def create_study(body: ImagingStudyIn, response: Response):
    pid = ref_id(body.patientRef, "Patient")
    if pid not in patients:
        outcome(400, "invalid", f"Patient/{pid} no existe")
    numbers = [sr.number for sr in body.series]
    if len(set(numbers)) != len(numbers):
        outcome(400, "invalid", "Números de serie repetidos")
    for sr in body.series:
        if sr.deviceId and sr.deviceId not in devices:
            outcome(400, "invalid", f"Device/{sr.deviceId} no existe")

    sid = str(uuid4())
    studies[sid] = {
        "uid": f"2.25.{uuid4().int}",
        "patientRef": body.patientRef,
        "started": body.started.isoformat(),
        "description": body.description,
        "series": [sr.model_dump() for sr in body.series],
    }
    response.headers["Location"] = f"/fhir/ImagingStudy/{sid}"
    return render_study(sid)


@app.get("/fhir/ImagingStudy", tags=["3. Estudios de RM"])
def search_studies(patient: Optional[str] = None):
    ids = [sid for sid, s in studies.items() if patient is None or s["patientRef"] == patient]
    return bundle([render_study(i) for i in ids])


@app.get("/fhir/ImagingStudy/{sid}", tags=["3. Estudios de RM"])
def get_study(sid: str):
    if sid not in studies:
        outcome(404, "not-found", f"ImagingStudy/{sid} no existe")
    return render_study(sid)


@app.get("/fhir/ImagingStudy/{sid}/instances", tags=["3. Estudios de RM"])
def list_instances(sid: str):
    if sid not in studies:
        outcome(404, "not-found", f"ImagingStudy/{sid} no existe")
    items = [
        {"id": bid, "series": b["series"], "contentType": b["contentType"], "url": f"/fhir/Binary/{bid}"}
        for bid, b in binaries.items() if b["studyId"] == sid
    ]
    return bundle(items)


@app.delete("/fhir/ImagingStudy/{sid}", status_code=204, tags=["3. Estudios de RM"])
def delete_study(sid: str):
    if sid not in studies:
        outcome(404, "not-found", f"ImagingStudy/{sid} no existe")
    for bid in [b for b, v in binaries.items() if v["studyId"] == sid]:
        del binaries[bid]
    del studies[sid]
    return Response(status_code=204)


# ---------------- Binary (imágenes DICOM) ----------------

@app.post("/fhir/Binary", status_code=201, tags=["4. Imágenes (Binary)"])
async def upload_binary(
    request: Request,
    response: Response,
    studyId: str = Query(...),
    series: int = Query(..., ge=1),
):
    ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ct not in ALLOWED_TYPES:
        outcome(415, "not-supported", f"Tipo no permitido: '{ct or 'vacío'}'")
    if studyId not in studies:
        outcome(404, "not-found", f"ImagingStudy/{studyId} no existe")
    if series not in {sr["number"] for sr in studies[studyId]["series"]}:
        outcome(400, "invalid", f"La serie {series} no existe en el estudio")

    data = await request.body()
    if not data:
        outcome(400, "invalid", "Cuerpo vacío")
    if ct == "application/dicom" and data[128:132] != b"DICM":
        outcome(400, "invalid", "Falta la cabecera DICOM")
    if ct == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        outcome(400, "invalid", "No es un PNG válido")

    bid = str(uuid4())
    binaries[bid] = {"contentType": ct, "data": data, "studyId": studyId, "series": series}
    response.headers["Location"] = f"/fhir/Binary/{bid}"
    return {"resourceType": "Binary", "id": bid, "contentType": ct, "size": len(data)}


@app.get("/fhir/Binary/{bid}", tags=["4. Imágenes (Binary)"])
def get_binary(bid: str):
    if bid not in binaries:
        outcome(404, "not-found", f"Binary/{bid} no existe")
    b = binaries[bid]
    return Response(content=b["data"], media_type=b["contentType"])


# ---------------- Vista previa para el navegador ----------------

@app.get("/preview/{bid}", tags=["5. Visor"])
def preview(bid: str):
    if bid not in binaries:
        outcome(404, "not-found", f"Binary/{bid} no existe")
    b = binaries[bid]
    if b["contentType"] == "image/png":
        return Response(content=b["data"], media_type="image/png")
    ds = pydicom.dcmread(BytesIO(b["data"]))
    arr = ds.pixel_array.astype(np.float32)
    lo, hi = float(arr.min()), float(arr.max())
    escala = (arr - lo) / (hi - lo + 1e-9) * 255
    buf = BytesIO()
    Image.fromarray(escala.astype(np.uint8)).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


VIEWER = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Estudios de resonancia magnética</title>
<style>
body{font-family:system-ui,sans-serif;background:#f4f6f8;margin:0;padding:20px;color:#222}
h1{margin-top:0}
.card{background:#fff;border-radius:10px;padding:16px;margin-bottom:24px;
  box-shadow:0 1px 4px rgba(0,0,0,.1);display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:700px){.card{grid-template-columns:1fr}}
.card p{margin:6px 0}
.imagen img{width:100%;max-width:380px;background:#000;border-radius:6px;display:block}
input[type=range]{width:100%;max-width:380px}
small{color:#666}
</style></head><body>
<h1>Estudios de resonancia magnética</h1>
<div id="lista">Cargando…</div>
<script>
const esc = t => String(t ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function cargar() {
  const lista = document.getElementById("lista");
  const estudios = (await (await fetch("/fhir/ImagingStudy")).json()).entry || [];
  lista.innerHTML = "";
  if (estudios.length === 0) {
    lista.textContent = "No hay estudios. Corra simulate.py primero.";
    return;
  }
  for (const e of estudios) {
    const s = e.resource;
    const pac = await (await fetch("/fhir/" + s.subject.reference)).json();
    const inst = ((await (await fetch(`/fhir/ImagingStudy/${s.id}/instances`)).json()).entry || [])
      .map(x => x.resource);

    const dispId = s.series[0]?.performer?.[0]?.actor?.reference?.split("/")[1];
    let equipo = "No registrado";
    if (dispId) {
      const disp = await (await fetch("/fhir/Device/" + dispId)).json();
      equipo = disp.type.text + " — " + disp.manufacturer + " " + disp.deviceName[0].name +
        " (serie " + (disp.serialNumber || "no registrada") + ")";
    }

    const nombre = pac.name[0].given[0] + " " + pac.name[0].family;
    const hc = pac.identifier[0].value;
    const fecha = new Date(s.started).toLocaleString("es-CO", {dateStyle: "long", timeStyle: "short"});
    const series = s.series.map(sr => esc(sr.description) + " (" + sr.numberOfInstances + " cortes)").join("<br>");

    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div>
        <h2>${esc(nombre)}</h2>
        <p><b>Historia clínica:</b> ${esc(hc)}</p>
        <p><b>Sexo:</b> ${esc(pac.gender)} &middot; <b>Nacimiento:</b> ${esc(pac.birthDate)}</p>
        <hr>
        <p><b>Fecha del estudio:</b> ${esc(fecha)}</p>
        <p><b>Equipo:</b> ${esc(equipo)}</p>
        <p><b>Series:</b><br>${series}</p>
        <p><b>Descripción:</b> ${esc(s.description)}</p>
      </div>
      <div class="imagen">
        <img id="img-${s.id}" alt="Corte de resonancia">
        <p><small id="txt-${s.id}"></small></p>
        <input type="range" id="sl-${s.id}" min="0" max="${Math.max(inst.length - 1, 0)}" value="0">
      </div>`;
    lista.appendChild(card);

    const img = document.getElementById("img-" + s.id);
    const txt = document.getElementById("txt-" + s.id);
    const sl = document.getElementById("sl-" + s.id);
    const mostrar = i => {
      if (!inst.length) { txt.textContent = "Sin imágenes"; return; }
      img.src = "/preview/" + inst[i].id;
      txt.textContent = "Serie " + inst[i].series + " · corte " + (i + 1) + " de " + inst.length;
    };
    sl.oninput = () => mostrar(+sl.value);
    mostrar(0);
  }
}
cargar();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse, tags=["5. Visor"])
def viewer():
    return VIEWER
