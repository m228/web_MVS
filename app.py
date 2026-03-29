from fastapi import FastAPI
import camera_core as core

app = FastAPI()

@app.get("/")
def main():
    core.load_driver()
    if core.check():
        return {"status": "ok"}
    else:
        return {"status": "error"}

@app.get("/cams")
def cams():
    cams = core.scan_cams()
    return cams
