"""Static catalog of the four tables the agent may use.

It is the single source of truth for: the SQL guard (column qualification / star expansion), the
`describe_schema` tool, and the offline DuckDB generator. `scripts/smoke_online.py` checks it
against the live BigQuery schema so drift is caught.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # BigQuery type
    description: str


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: tuple[Column, ...]
    grain: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


C = Column

TABLES: dict[str, Table] = {
    "orders": Table(
        name="orders",
        description="One row per customer order (order header).",
        grain="order_id",
        columns=(
            C("order_id", "INT64", "Order identifier."),
            C("user_id", "INT64", "Customer who placed the order (pseudonymised in results)."),
            C("status", "STRING", "Processing | Shipped | Complete | Cancelled | Returned."),
            C("gender", "STRING", "Customer gender copied onto the order (M/F). Aggregate-only."),
            C("created_at", "TIMESTAMP", "When the order was placed (UTC)."),
            C("returned_at", "TIMESTAMP", "When the order was returned, if returned."),
            C("shipped_at", "TIMESTAMP", "When the order shipped."),
            C("delivered_at", "TIMESTAMP", "When the order was delivered."),
            C("num_of_item", "INT64", "Number of items in the order."),
        ),
        notes=("Money is NOT on this table; revenue comes from order_items.sale_price.",),
    ),
    "order_items": Table(
        name="order_items",
        description="One row per item sold. The fact table for revenue.",
        grain="id",
        columns=(
            C("id", "INT64", "Order item identifier."),
            C("order_id", "INT64", "FK -> orders.order_id."),
            C("user_id", "INT64", "FK -> users.id (pseudonymised in results)."),
            C("product_id", "INT64", "FK -> products.id."),
            C("inventory_item_id", "INT64", "Inventory unit that was sold."),
            C("status", "STRING", "Processing | Shipped | Complete | Cancelled | Returned."),
            C("created_at", "TIMESTAMP", "When the item was ordered (UTC)."),
            C("shipped_at", "TIMESTAMP", "When the item shipped."),
            C("delivered_at", "TIMESTAMP", "When the item was delivered."),
            C("returned_at", "TIMESTAMP", "When the item was returned."),
            C("sale_price", "FLOAT64", "Price paid for the item (USD). Revenue = SUM(sale_price)."),
        ),
        notes=(
            "Net revenue conventionally excludes items with status Cancelled or Returned.",
            "Gross margin = sale_price - products.cost.",
        ),
    ),
    "products": Table(
        name="products",
        description="Product catalog.",
        grain="id",
        columns=(
            C("id", "INT64", "Product identifier."),
            C("cost", "FLOAT64", "Unit cost to the company (USD)."),
            C("category", "STRING", "Product category, e.g. Jeans, Outerwear & Coats, Swim."),
            C("name", "STRING", "Product name."),
            C("brand", "STRING", "Brand name."),
            C("retail_price", "FLOAT64", "List price (USD)."),
            C("department", "STRING", "Men | Women."),
            C("sku", "STRING", "Stock keeping unit."),
            C("distribution_center_id", "INT64", "Distribution center that stocks the product."),
        ),
    ),
    "users": Table(
        name="users",
        description="Customers. Personal fields are NOT available to the agent.",
        grain="id",
        columns=(
            C("id", "INT64", "Customer identifier (pseudonymised in results)."),
            C("first_name", "STRING", "PII - not available."),
            C("last_name", "STRING", "PII - not available."),
            C("email", "STRING", "PII - not available."),
            C("age", "INT64", "Age in years. Aggregate-only."),
            C("gender", "STRING", "M/F. Aggregate-only."),
            C("state", "STRING", "State / province. Aggregate-only."),
            C("street_address", "STRING", "PII - not available."),
            C("postal_code", "STRING", "PII - not available."),
            C("city", "STRING", "City. Aggregate-only."),
            C("country", "STRING", "Country. Aggregate-only."),
            C("latitude", "FLOAT64", "PII - not available."),
            C("longitude", "FLOAT64", "PII - not available."),
            C("traffic_source", "STRING", "Acquisition channel: Search, Organic, Facebook, Email, Display. Aggregate-only."),
            C("created_at", "TIMESTAMP", "Customer sign-up time (UTC)."),
            C("user_geom", "GEOGRAPHY", "PII - not available."),
        ),
    ),
}

RELATIONSHIPS = (
    "order_items.order_id = orders.order_id",
    "order_items.user_id = users.id",
    "orders.user_id = users.id",
    "order_items.product_id = products.id",
)

# BigQuery type -> DuckDB type (offline backend)
DUCKDB_TYPES = {
    "INT64": "BIGINT",
    "FLOAT64": "DOUBLE",
    "STRING": "VARCHAR",
    "TIMESTAMP": "TIMESTAMP",
    "GEOGRAPHY": "VARCHAR",
}
