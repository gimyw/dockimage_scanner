from fastapi import FastAPI


app = FastAPI()


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "imgadvisor-fastapi-test"}
