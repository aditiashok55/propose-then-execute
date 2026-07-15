"""
Simulates the failure: opens N connections, starts a transaction on each,
runs one query, and then just... holds them open without committing.
That's exactly what produces idle-in-transaction sessions in the real
world (an app that opens a transaction and forgets to close it, a long
HTTP request holding a connection, etc).

Run this in one terminal, then run loop/harness.py in another.
"""

import argparse
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def hold_connection(index: int, hold_seconds: int):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1")
            # Deliberately misbehaving: holding the transaction open
            # without committing, simulating a forgotten connection.
            print(f"[conn {index}] opened, holding idle-in-transaction for {hold_seconds}s")
            await asyncio.sleep(hold_seconds)
    finally:
        await conn.close()
        print(f"[conn {index}] closed")


async def main(num_connections: int, hold_seconds: int):
    print(f"Opening {num_connections} idle-in-transaction connections for {hold_seconds}s each...")
    await asyncio.gather(*[hold_connection(i, hold_seconds) for i in range(num_connections)])
    print("All seeded connections closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed idle-in-transaction connections.")
    parser.add_argument("--connections", type=int, default=15)
    parser.add_argument("--hold-seconds", type=int, default=120)
    args = parser.parse_args()
    asyncio.run(main(args.connections, args.hold_seconds))
