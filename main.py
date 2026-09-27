import uvicorn

from app.api import create_app
from app.logging_config import configure_logging

configure_logging()
app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
