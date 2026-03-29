from fastapi import FastAPI

import camera_core
import camera_core as core

app = FastAPI()

@app.get("/")
def main():
    if camera_core.check():
        return {"status": "ok"}

@app.get("/cams")
def cams():
    output = core.scan_cams()
    return output
