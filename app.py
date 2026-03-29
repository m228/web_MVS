from fastapi import FastAPI
import camera_core as core

app = FastAPI()

@app.get("/")
def home2():
    return {"message": "Hello World"}

@app.get("/cams")
def cams():
    output = core.scan_cams()
    return output
