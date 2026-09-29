"""Lambda entrypoint for the API function."""

from mangum import Mangum

from api.main import app

handler = Mangum(app, lifespan="off")
