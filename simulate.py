"""Cliente simulado: crea pacientes, equipos, estudios e imágenes DICOM visibles."""
import io
import random
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

BASE = "https://api-resonancia.onrender.com/fhir"  # sin barra al final
N_PACIENTES = 5
ESTUDIOS_POR_PACIENTE = 2
CORTES = 12
TAM = 256
random.seed(42)
rng = np.random.default_rng(42)

NOMBRES = ["Laura", "Carlos", "María", "Andrés", "Sofía", "Juan"]
APELLIDOS = ["Gómez", "Rodríguez", "Martínez", "López", "Ramírez"]

http = httpx.Client(base_url=BASE, timeout=60)


def dicom_imagen(corte: int, total: int, paciente: dict) -> bytes:
    """Genera un corte DICOM con una elipse que cambia según el corte."""
    y, x = np.ogrid[:TAM, :TAM]
    c = TAM // 2
    z = (corte / max(total - 1, 1)) * 2 - 1
    radio = TAM * 0.40 * np.sqrt(max(0.0, 1 - z ** 2))
    dist = np.sqrt((x - c) ** 2 + (y - c) ** 2)

    img = np.zeros((TAM, TAM), dtype=np.float32)
    img[dist < radio] = 900
    img[dist < radio * 0.6] = 1500
    img += rng.normal(0, 25, (TAM, TAM)).astype(np.float32)
    img = np.clip(img, 0, 4095).astype(np.uint16)

    uid = generate_uid()
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\x00" * 128)
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = uid
    ds.Modality = "MR"
    ds.PatientName = paciente["nombre"].replace(" ", "^")
    ds.PatientID = paciente["hc"]
    ds.InstanceNumber = corte + 1
    ds.Rows = TAM
    ds.Columns = TAM
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.PixelData = img.tobytes()
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    buf = io.BytesIO()
    ds.save_as(buf)
    return buf.getvalue()


def main():
    # 1. Equipos
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

    # 2. Pacientes
    pacientes = []
    for p in range(N_PACIENTES):
        nombre = random.choice(NOMBRES)
        apellido = random.choice(APELLIDOS)
        body = {
            "family": apellido,
            "given": nombre,
            "gender": random.choice(["male", "female"]),
            "birthDate": f"{random.randint(1950, 2000)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
            "identifier": f"HC-{100000 + p}",
        }
        r = http.post("/Patient", json=body)
        r.raise_for_status()
        pacientes.append({
            "ref": f"Patient/{r.json()['id']}",
            "nombre": f"{nombre} {apellido}",
            "hc": body["identifier"],
        })

    # 3. Estudios e imágenes
    series_plan = [(1, "T1 axial"), (2, "T2 axial")]
    ahora = datetime.now(timezone(timedelta(hours=-5)))
    subidas = 0
    ultimo_sid = None

    for pac in pacientes:
        for _ in range(ESTUDIOS_POR_PACIENTE):
            dispositivo = random.choice(device_ids)
            inicio = ahora - timedelta(days=random.randint(0, 30), hours=random.randint(0, 8))
            body = {
                "patientRef": pac["ref"],
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
                for corte in range(CORTES):
                    up = http.post(
                        "/Binary",
                        params={"studyId": sid, "series": n},
                        content=dicom_imagen(corte, CORTES, pac),
                        headers={"Content-Type": "application/dicom"},
                    )
                    up.raise_for_status()
                    subidas += 1

    # 4. Casos que deben fallar
    r = http.post("/Binary", params={"studyId": ultimo_sid, "series": 1},
                  content=b"no soy dicom", headers={"Content-Type": "application/dicom"})
    print("Archivo sin cabecera DICOM ->", r.status_code, "(esperado 400)")
    r = http.get("/Patient/no-existe")
    print("Paciente inexistente       ->", r.status_code, "(esperado 404)")

    # 5. Verificación
    estudios = http.get("/ImagingStudy").json()
    instancias_api = sum(e["resource"]["numberOfInstances"] for e in estudios["entry"])
    print(f"\nPacientes creados: {len(pacientes)}")
    print(f"Estudios creados: {estudios['total']}")
    print(f"Instancias subidas: {subidas} | Instancias en la API: {instancias_api}")
    assert subidas == instancias_api, "No cuadran las instancias"


if __name__ == "__main__":
    main()
