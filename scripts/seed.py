"""Insert sample records without overwriting existing records or reducing stock."""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.models import Cart, CartItem, Category, Order, Product
from app.db.schema import create_schema
from app.db.session import get_engine

DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "lifestore_sample_db.json"


def seed_database(engine: Engine, source: Path = DEFAULT_SOURCE) -> dict[str, int]:
    data = json.loads(source.read_text(encoding="utf-8"), parse_float=Decimal)
    inserted = {key: 0 for key in ("categories", "products", "carts", "orders")}
    # One transaction for the entire dataset: invalid references roll everything back.
    with Session(engine) as session, session.begin():
        for key, model in (("categories", Category), ("products", Product), ("carts", Cart), ("orders", Order)):
            for record in data[key]:
                if session.get(model, record["id"]) is not None:
                    continue
                fields = dict(record)
                if model is Cart:
                    fields["items"] = [CartItem(**item) for item in fields["items"]]
                session.add(model(**fields))
                inserted[key] += 1
            # Insert parents before rows referring to them.
            session.flush()
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()
    engine = get_engine()
    try:
        create_schema(engine)
        print("Inserted: " + json.dumps(seed_database(engine, args.source)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
