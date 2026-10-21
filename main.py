from fastapi import FastAPI
from routes import router

app = FastAPI(title="Smart File Handler")

app.include_router(router)
