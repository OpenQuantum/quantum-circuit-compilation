import argparse

import uvicorn
from star_bff.config import Settings as StarSettings

from .config import Settings
from .main import create_app


def main():
    parser = argparse.ArgumentParser(description="Quantum competition BFF and local frontend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8320)
    args = parser.parse_args()
    uvicorn.run(create_app(StarSettings(), Settings()), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
