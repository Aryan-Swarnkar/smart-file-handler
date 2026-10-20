from fastapi import FastAPI
from routes import router

app = FastAPI(title="Smart File Handler")

app.include_router(router)

if __name__ == "__main__":
    app.run("uv run python dev main.py")