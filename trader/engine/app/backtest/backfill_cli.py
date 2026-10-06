import asyncio, sys
from app.config import settings
from app.data.ingest import backfill_universe
from app.db.session import init_db
from app.exchange.registry import make_client

async def main():
    tfs = sys.argv[1].split(",")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    await init_db()
    client = make_client()
    await client.sync_time()
    await backfill_universe(client, settings.symbols, tfs, n)
    await client.aclose()

asyncio.run(main())
