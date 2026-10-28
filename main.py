from datetime import datetime
from typing import Optional
from uuid import uuid4
 
from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
 
app = FastAPI(title="API FHIR de imágenes por resonancia", version="2.0.0")
 
DCM = "http://dicom.nema.org/resources/ontology/DCM"
ALLOWED_TYPES = {"application/dicom", "image/png"}
 
devices: dict[str, dict] = {}
studies: dict[str, dict] = {}   # id -> datos de entrada + UID DICOM
binaries: dict[str, dict] = {}  # id -> contenido, tipo y estudio al que pertenece
 
 
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
 
 
# ---------------- Device ----------------
 
class DeviceIn(BaseModel):
    status: str = Field("active", pattern="^(active|inactive|entered-in-error|unknown)$")
    typeText: str
    manufacturer: str
    modelName: str
    serialNumber: Optional[str] = None
 
 
def device_resource(device_id: str, d: DeviceIn) -> dict:
    r = {
        "resourceType": "Device",
        "id": device_id,
        "status": d.status,
        "type": {"text": d.typeText},
        "manufacturer": d.manufacturer,
        "deviceName": [{"name": d.modelName, "type": "model-name"}],
    }
    if d.serialNumber:
        r["serialNumber"] = d.serialNumber
    return r
 
 
@app.post("/fhir/Device", status_code=201)
def create_device(body: DeviceIn, response: Response):
    did = str(uuid4())
    devices[did] = device_resource(did, body)
    response.headers["Location"] = f"/fhir/Device/{did}"
    return devices[did]
 
 
@app.get("/fhir/Device")
def search_devices(manufacturer: Optional[str] = None):
    items = [
        d for d in devices.values()
        if manufacturer is None or d["manufacturer"].lower() == manufacturer.lower()
    ]
    return bundle(items)
 
 
@app.get("/fhir/Device/{device_id}")
def get_device(device_id: str):
    if device_id not in devices:
        outcome(404, "not-found", f"Device/{device_id} no existe")
    return devices[device_id]
 
 
@app.put("/fhir/Device/{device_id}")
def update_device(device_id: str, body: DeviceIn):
    if device_id not in devices:
        outcome(404, "not-found", f"Device/{device_id} no existe")
    devices[device_id] = device_resource(device_id, body)
    return devices[device_id]
 
 
@app.delete("/fhir/Device/{device_id}", status_code=204)
def delete_device(device_id: str):
    if device_id not in devices:
        outcome(404, "not-found", f"Device/{device_id} no existe")
    if any(sr["deviceId"] == device_id for s in studies.values() for sr in s["series"]):
        outcome(409, "conflict", "El dispositivo está referenciado por estudios")
    del devices[device_id]
    return Response(status_code=204)
 
 
# ---------------- ImagingStudy ----------------
 
class SeriesIn(BaseModel):
    number: int = Field(..., ge=1)
    modality: str = Field("MR", examples=["MR"])
    description: str
    deviceId: Optional[str] = None
 
 
class ImagingStudyIn(BaseModel):
    patientRef: str = Field(..., pattern=r"^Patient/[A-Za-z0-9\-.]+$", examples=["Patient/pac-001"])
    started: datetime
    modality: str = "MR"
    description: str = ""
    series: list[SeriesIn] = Field(..., min_length=1)
 
 
def render_study(sid: str) -> dict:
    s = studies[sid]
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
            item["performer"] = [{"actor": {"reference": f"Device/{sr['deviceId']}"}}]
        series_out.append(item)
    return {
        "resourceType": "ImagingStudy",
        "id": sid,
        "status": "available" if inst else "registered",
        "subject": {"reference": s["patientRef"]},
        "started": s["started"],
        "description": s["description"],
        "identifier": [{"system": "urn:dicom:uid", "value": f"urn:oid:{s['uid']}"}],
        "modality": [{"system": DCM, "code": s["modality"]}],
        "numberOfSeries": len(series_out),
        "numberOfInstances": len(inst),
        "series": series_out,
    }
 
 
@app.post("/fhir/ImagingStudy", status_code=201)
def create_study(body: ImagingStudyIn, response: Response):
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
        "modality": body.modality,
        "description": body.description,
        "series": [sr.model_dump() for sr in body.series],
    }
    response.headers["Location"] = f"/fhir/ImagingStudy/{sid}"
    return render_study(sid)
 
 
@app.get("/fhir/ImagingStudy")
def search_studies(patient: Optional[str] = None, modality: Optional[str] = None):
    ids = [
        sid for sid, s in studies.items()
        if (patient is None or s["patientRef"] == patient)
        and (modality is None or s["modality"].upper() == modality.upper())
    ]
    return bundle([render_study(i) for i in ids])
 
 
@app.get("/fhir/ImagingStudy/{sid}")
def get_study(sid: str):
    if sid not in studies:
        outcome(404, "not-found", f"ImagingStudy/{sid} no existe")
    return render_study(sid)
 
 
@app.get("/fhir/ImagingStudy/{sid}/instances")
def list_instances(sid: str):
    if sid not in studies:
        outcome(404, "not-found", f"ImagingStudy/{sid} no existe")
    items = [
        {"id": bid, "series": b["series"], "contentType": b["contentType"], "url": f"/fhir/Binary/{bid}"}
        for bid, b in binaries.items() if b["studyId"] == sid
    ]
    return bundle(items)
 
 
@app.delete("/fhir/ImagingStudy/{sid}", status_code=204)
def delete_study(sid: str):
    if sid not in studies:
        outcome(404, "not-found", f"ImagingStudy/{sid} no existe")
    for bid in [b for b, v in binaries.items() if v["studyId"] == sid]:
        del binaries[bid]
    del studies[sid]
    return Response(status_code=204)
 
 
# ---------------- Binary (pixeles) ----------------
 
@app.post("/fhir/Binary", status_code=201)
async def upload_binary(
    request: Request,
    response: Response,
    studyId: str = Query(...),
    series: int = Query(..., ge=1),
):
    ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ct not in ALLOWED_TYPES:
        outcome(415, "not-supported", f"Tipo no permitido: '{ct or 'vacío'}'. Usa application/dicom o image/png")
    if studyId not in studies:
        outcome(404, "not-found", f"ImagingStudy/{studyId} no existe")
    if series not in {sr["number"] for sr in studies[studyId]["series"]}:
        outcome(400, "invalid", f"La serie {series} no existe en el estudio")
 
    data = await request.body()
    if not data:
        outcome(400, "invalid", "Cuerpo vacío")
    if ct == "application/dicom" and data[128:132] != b"DICM":
        outcome(400, "invalid", "Falta la cabecera DICOM (DICM en el byte 128)")
    if ct == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        outcome(400, "invalid", "No es un PNG válido")
 
    bid = str(uuid4())
    binaries[bid] = {"contentType": ct, "data": data, "studyId": studyId, "series": series}
    response.headers["Location"] = f"/fhir/Binary/{bid}"
    return {"resourceType": "Binary", "id": bid, "contentType": ct, "size": len(data)}
 
 
@app.get("/fhir/Binary/{bid}")
def get_binary(bid: str):
    if bid not in binaries:
        outcome(404, "not-found", f"Binary/{bid} no existe")
    b = binaries[bid]
    return Response(content=b["data"], media_type=b["contentType"])
Page Redirection to current version of DICOM Controlled Terminology Definitions
 
