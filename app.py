from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import camera_core as core


@asynccontextmanager
async def lifespan(app: FastAPI):
    core.load_driver()
    core.scan_cams()
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="page/static"), name="static")

@app.get("/")
def home():
    return FileResponse("page/index.html")

@app.get("/cams")
def cams():
    return FileResponse("page/cams.html")

@app.get("/camera")
def camera():
    return FileResponse("page/camera.html")




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

@app.get("/api/count_cams")
def count_cams():
    return core.count_cams()





#@app.post("/api/camera/settings")
#def camera_settings():
#    return null
