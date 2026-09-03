from fastapi import FastAPI

app = FastAPI(title="Homelab API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
