import os

from helper.control import main


if __name__ == "__main__":
    os.environ.setdefault("PUBLIC_WORKER_URL", "https://rakcrmphelper.bothost.tech")
    os.environ.setdefault("LISTEN_HOST", "0.0.0.0")
    os.environ.setdefault("PORT", "3000")
    main()
