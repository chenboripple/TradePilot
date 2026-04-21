from fastapi import FastAPI

app = FastAPI(title="ripple_tradePilot")


@app.get("/health")
def health():
    return {"status": "ok"}
