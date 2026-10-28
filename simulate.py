"""Cliente simulado: crea dispositivos, estudios y sube imágenes falsas."""
import random
from datetime import datetime, timedelta, timezone
 
import httpx
 
BASE = "http://127.0.0.1:8000/fhir"
N_PACIENTES = 5
ESTUDIOS_POR_PACIENTE = 2
random.seed(42)  # resultados reproducibles
 
http = httpx.Client(base_url=BASE, timeout=30)
 
 
def dicom_falso(tam: int = 20_000) -> bytes:
    """Cabecera DICOM válida + ruido. Pasa la validación, pero NO es una imagen visible."""
    return b"\x00" * 128 + b"DICM" + random.randbytes(tam)
 
 
def main():
    equipos = [
        {"typeText": "Resonancia magnética 3T", "manufacturer": "Siemens",
         "modelName": "MAGNETOM Skyra", "serialNumber": "SN-3T-001"},
        {"typeText": "Resonancia magnética 1.5T", "manufacturer": "Philips",
         "modelName": "Ingenia", "serialNumber": "SN-15T-002"},
    ]
    device_ids = []
    for e in equipos:
        r = http.post("/Device", json=e)
        r.raise_for_status()
        device_ids.append(r.json()["id"])
 
    series_plan = [(1, "T1 axial"), (2, "T2 axial")]
    ahora = datetime.now(timezone(timedelta(hours=-5)))
    subidas = 0
    ultimo_sid = None
 
    for p in range(1, N_PACIENTES + 1):
        paciente = f"Patient/pac-{p:03d}"
        for _ in range(ESTUDIOS_POR_PACIENTE):
            dispositivo = random.choice(device_ids)
            inicio = ahora - timedelta(days=random.randint(0, 30), hours=random.randint(0, 8))
            body = {
                "patientRef": paciente,
                "started": inicio.isoformat(),
                "description": "RM cerebro simulada",
                "series": [
                    {"number": n, "modality": "MR", "description": d, "deviceId": dispositivo}
                    for n, d in series_plan
                ],
            }
            r = http.post("/ImagingStudy", json=body)
            r.raise_for_status()
            ultimo_sid = sid = r.json()["id"]
 
            for n, _ in series_plan:
                for _ in range(random.randint(3, 6)):
                    up = http.post(
                        "/Binary",
                        params={"studyId": sid, "series": n},
                        content=dicom_falso(),
                        headers={"Content-Type": "application/dicom"},
                    )
                    up.raise_for_status()
                    subidas += 1
 

    r = http.post("/Binary", params={"studyId": ultimo_sid, "series": 1},
                  content=b"no soy dicom", headers={"Content-Type": "application/dicom"})
    print("Archivo sin cabecera DICOM ->", r.status_code, "(esperado 400)")
 
    r = http.get("/ImagingStudy/no-existe")
    print("Estudio inexistente        ->", r.status_code, "(esperado 404)")
 

    bundle_mr = http.get("/ImagingStudy", params={"modality": "MR"}).json()
    instancias_api = sum(e["resource"]["numberOfInstances"] for e in bundle_mr["entry"])
    print(f"\nEstudios creados: {bundle_mr['total']}")
    print(f"Instancias subidas: {subidas} | Instancias en la API: {instancias_api}")
    assert subidas == instancias_api, "No cuadran las instancias"
 
    for e in bundle_mr["entry"][:3]:
        res = e["resource"]
        print(res["id"], res["subject"]["reference"], res["numberOfInstances"], res["status"])
 
 
if __name__ == "__main__":
    main()
 
