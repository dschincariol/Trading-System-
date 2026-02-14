from engine.runtime.supervisor import RuntimeSupervisor
import time


def main():
    supervisor = RuntimeSupervisor()

    # Register jobs here
    supervisor.register_job(
        name="price_stream",
        script="backend/stream_prices_polygon_ws.py",
        daemon=True
    )

    supervisor.register_job(
        name="options_poll",
        script="backend/options_poll.py",
        daemon=False
    )

    # Start core daemons
    supervisor.start("price_stream")

    print("Engine started.")
    print("Press Ctrl+C to exit.")

    try:
        while True:
            time.sleep(5)
            print(supervisor.status())
    except KeyboardInterrupt:
        print("Stopping...")
        supervisor.stop("price_stream")


if __name__ == "__main__":
    main()
