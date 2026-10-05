# Phase 1 schema

This phase uses SQLAlchemy `Base.metadata.create_all`, as permitted by the plan.

Run `docker compose run --rm api python -m app.db.schema` to create missing tables.
The seed command also creates missing tables before inserting data. Neither command
changes existing columns; later schema changes will need versioned migrations.

The four JSON collections map to categories, products, carts, and orders. Nested
cart items map to cart_items, with `(cart_id, product_id)` as the composite key.
`cart_id` records the JSON parent relationship. No timestamps, generated item IDs,
or additional business fields are introduced. Money uses NUMERIC(12, 2).
