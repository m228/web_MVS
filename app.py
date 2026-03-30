from fastapi import FastAPI
from fastapi.responses import FileResponse
import camera_core as core
app = FastAPI()

@app.on_event("startup")
def startup():
    core.load_driver()

@app.get("/")
def home():
    return FileResponse("page/index.html")

@app.get("/cams")
def cams():
    return FileResponse("page/cams.html")



@app.get("/api/cams")
def api_cams():
    return core.scan_cams()

@app.get("/api/status")
def api_status():
    try:
        return {"status": core.check()}
    except Exception as e:
        return {"status": False, "error": str(e)}

@app.get("/api/ip")
def get_ip(serial_number: str):
    return core.get_ip(serial_number)


