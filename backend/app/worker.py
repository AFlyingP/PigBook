import logging
import signal
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker")

_running = True


def _handle_signal(signum: int, frame: object) -> None:
    global _running
    logger.info("Shutdown signal received (%s), exiting worker cleanly...", signum)
    _running = False


def main() -> None:
    # Idle worker process. No background jobs are registered yet.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Worker started in idle loop mode")
    try:
        while _running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, exiting worker cleanly...")
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
