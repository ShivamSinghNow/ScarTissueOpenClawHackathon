from __future__ import annotations

from collections.abc import Callable

import modal

APP_NAME = "scartissue-backend"
CHROMA_VOLUME_NAME = "scartissue-chroma-db"
SECRETS_NAME = "scartissue-keys"

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install_from_pyproject("pyproject.toml")
    .add_local_python_source("app")
)

app = modal.App(APP_NAME)
chroma_volume = modal.Volume.from_name(CHROMA_VOLUME_NAME, create_if_missing=True)
api_secrets = modal.Secret.from_name(SECRETS_NAME)


@app.function(
    image=image,
    secrets=[api_secrets],
    volumes={"/data/chroma_db": chroma_volume},
    env={
        "CHROMA_PERSIST_DIR": "/data/chroma_db",
        "GIT_PYTHON_GIT_EXECUTABLE": "/usr/bin/git",
    },
    memory=4096,
    timeout=300,
    min_containers=1,
)
@modal.asgi_app()
def fastapi_app() -> Callable:
    from app.main import app as fastapi_application

    return fastapi_application
